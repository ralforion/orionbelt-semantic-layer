# Federated Queries

A model's data objects can live in different databases. A query that reads
data objects of more than one [data source](data-sources.md) is a *federated
query*: OBSL splits it into one query per source, runs them at once, and
combines their results in an in-memory DuckDB. The answer is the one a single
database holding all the tables would return, or the query is refused with a
named `FEDERATION_*` error. It is never answered with a different number.

This page explains how a query is split, where each filter runs, how each
aggregation is combined, what the run time settings are, and every
[limitation](#limitations).

## Declaring the sources

Each data object names the data source its table is read from, in
`dataSource`, which overrides the model's `settings.defaultDataSource`:

```yaml
settings:
  defaultDataSource: shop        # MySQL
dataObjects:
  Orders:
    code: orders                 # read from shop
    joins:
      - {joinType: many-to-one, joinTo: Customers,
         columnsFrom: [Customer], columnsTo: [Customer ID]}
  Customers:
    code: customers
    dataSource: crm              # PostgreSQL
```

The sources themselves are configured by name (`OBSL_DATABASES=shop,crm`,
`DB_SHOP_TYPE=mysql`, ...); see [Data Sources](data-sources.md). A query on
such a model that reads one source only is not split: it runs on that source,
in its dialect.

## How a query is split

```mermaid
flowchart LR
    Q[Query] --> P[Planner]
    P --> F["Fact leg<br/>(source of the measures)"]
    P --> D1["Dimension leg<br/>(another source)"]
    P --> D2["Dimension leg<br/>(another source)"]
    F --> C["Combine query<br/>(in-memory DuckDB)"]
    D1 --> C
    D2 --> C
    C --> R[Result]
```

- **Islands.** The query's data objects that share a source and are joined
  among themselves form an *island*. The island the measures' values come
  from, which also holds the query's base object, is the *fact island* (in a
  [query without measures](#queries-without-measures), the base object's);
  every other island is a *dimension island*, reached by one many-to-one join,
  a *federation edge*, from the fact island or from a dimension island nearer
  it ([a chain of sources](#chains-of-sources)). Measures of independent facts
  on several islands split the query into one query per fact first
  ([measures of independent facts](#measures-of-independent-facts)).
- **Legs.** Each island becomes a *leg*: an ordinary OBSL query against a model
  holding that island alone, written in its source's dialect. The **fact leg**
  groups by the query's dimensions on its island and by the edge keys (a small
  dimension leg's by a [map](#key-maps) of them), and
  pre-aggregates the measures, or returns its rows where a measure needs them
  ([computed from rows](#computed-from-rows)). A **dimension leg** returns the
  distinct keys with the dimensions the query reads there, the keys of the
  edges leaving it, and any column a measure reads there ([measures reading another source](#measures-reading-another-source)). A source not configured where the
  query is compiled gets the dialect the query is compiled for. Every leg
  returns its keys as stored, not in the model's `queryTimezone`, as one
  database's join compares them: two instants an hour apart are one
  wall-clock time in the hour a zone falls back.
- **Combine.** The combine query is the original query, compiled for DuckDB
  against a model whose data objects are the legs: each dimension leg is
  joined on its key, many-to-one, as `LEFT JOIN`, from the leg holding that
  key (the fact leg, or the dimension leg a chain passes through), or as
  `INNER JOIN` where the join is `required`, or the leg makes such a join
  within its source (one database's inner join to the regions after the left
  join to the customers drops the orders whose customer has no region), and each measure is
  re-aggregated from the fact leg's results. Derived metrics,
  [cumulative, period-over-period and window metrics](#metrics-across-periods),
  [reaggregate metrics](#reaggregate-metrics), HAVING, ORDER BY, LIMIT, totals, `rollup` / `cube` subtotals and a
  measure's [grain or filter context](#grain-and-filter-context) run there. A fact row whose key
  has no match keeps its values under an empty group, as in one database.

## An example

Revenue and orders by region, orders in MySQL (`shop`), customers in
PostgreSQL (`crm`), for the region EU:

```yaml
select:
  dimensions: [Region]
  measures: [Revenue, Orders Count]
where:
  - {field: Region, op: "=", value: EU}
```

The fact leg, on MySQL, sums and counts per customer:

```sql
SELECT `Orders`.`customer_id` AS `__fk0_0`, CAST(COUNT(1) AS SIGNED) AS `Orders Count`,
       SUM(`Orders`.`amount`) AS `Revenue`
FROM `orders` AS `Orders`
GROUP BY `Orders`.`customer_id`
ORDER BY `__fk0_0` ASC
LIMIT 5000001
```

The dimension leg, on PostgreSQL, returns the customers of the region, and
runs the filter itself:

```sql
SELECT "Customers"."region" AS "Region", "Customers"."customer_id" AS "__fk0_0"
FROM "customers" AS "Customers"
WHERE "Customers"."region" = 'EU'
GROUP BY "Customers"."region", "Customers"."customer_id"
ORDER BY "Region" ASC, "__fk0_0" ASC
LIMIT 5000001
```

The combine query, in DuckDB, joins them on the customer key, keeps the fact
rows the filtered leg matched, and adds up the partial results:

```sql
SELECT "leg_1"."Region" AS "Region", CAST(SUM("leg_0"."Revenue") AS DECIMAL(18, 2)) AS "Revenue",
       CAST(COALESCE(SUM("leg_0"."Orders Count"), 0) AS INTEGER) AS "Orders Count"
FROM "leg_0" AS "leg_0"
LEFT JOIN "leg_1" AS "leg_1" ON "leg_0"."__fk0_0" = "leg_1"."__fk0_0"
WHERE "leg_1"."__fk0_0" IS NOT NULL
GROUP BY ALL
```

Since the PostgreSQL leg runs a filter, the MySQL leg runs after it and reads
only the customers it returned ([key injection](#key-injection)): at run time
its `WHERE` gains `"customer_id" IN (...)`. `query/sql` and `obsl compile`
show the plan as compiled, before any key is known.

Each leg asks for one row more than `FEDERATION_MAX_LEG_ROWS` (default
5,000,000), so a leg past the limit is refused rather than combined in part.
It is read batch by batch: past `FEDERATION_SPILL_THRESHOLD_MB` (default 64)
it goes to disk where spilling is on, as it is by default, and a leg held in
memory is refused once it takes more than `FEDERATION_MAX_LEG_MB` (default
512) ([running a federated query](#running-a-federated-query)).

## Where to join the sources

A leg returns a row per key of each join between sources it holds, beside the
query's dimensions: the fact leg above sums per customer, not per region. Its
size follows how many keys the query reads, not how many groups the result
has. With 20,000,000 customers who ordered, revenue by region returns
20,000,000 rows from the shop for a result of eight, and is refused past
`FEDERATION_MAX_LEG_ROWS`. Without the filter on the region, no
[key injection](#key-injection) narrows it.

Where the sources meet decides what a query costs. Put a join between sources
on a dimension with few values that both share, a region, a product
category, a legal entity, a day, rather than on a key of single records, a
customer or an order:

| Join between the sources | The shop's leg for revenue by region |
|---|---|
| Orders -> Customers on the customer, the customers in the CRM | a row per customer who ordered |
| Customers -> Regions on the region code, orders and customers in the shop, the regions in the CRM | a row per region code |

A key of a source small enough to list, a catalog of 10,000 products, does
not decide the size where the fact leg also groups by something else: the
fact leg joins a map of its keys ([key maps](#key-maps)), and revenue by
category and day returns a row per category and day rather than per product
and day. A source past the [key injection](#key-injection) limit, the
20,000,000 customers, is not mapped.

Where a join must stay on such a key, filter the query on the other source's
dimensions, which narrows the keys the fact leg reads, or let legs grow: raise
`FEDERATION_MAX_LEG_ROWS`, and larger legs spill to disk
([running a federated query](#running-a-federated-query)). A leg refused for
its size names the joins whose keys it returns. A leg returning
rows rather than groups, of a [query of fields](#raw-mode) or of a measure
[computed from rows](#computed-from-rows), returns a row per row of its
source the query reads, which no placement of a join narrows: only a filter
does, and its refusal says so.

## Where filters run

| Filter | Runs in | Why |
|---|---|---|
| On the fact island's dimensions | the fact leg | as on one database |
| On one dimension island's dimensions, never true where they are all NULL (`=`, `!=`, `in`, comparisons, `like`, `relative`, ...) | that dimension leg, and the combine query keeps the fact rows it matched | compared as its own engine compares; the fact leg may then read only the keys it returned |
| True where the dimensions are NULL (`is_null`, `blank`, ...), or reading several islands (an OR across sources) | the combine query, after the join | in a dimension leg it would also match the fact rows whose key the leg dropped |
| After the join, on a dimension the query selects at a time grain (`Order Date:month`, filtered on `Order Date`) | the combine query, on a column the dimension's leg returns beside it: the dimension at its own grain (the day, or its declared `timeGrain`) | as on one database: `Order Date < 2024-01-15` compares each order's day, not the month selected |
| `relative`, after the join | the combine query, counting from the combine step's date | see [Results that can differ](#results-that-can-differ-from-one-database) |
| On the time dimension of a cumulative or period-over-period metric | the combine query | it says which periods to show, not which the metric reads: [metrics across periods](#metrics-across-periods) |
| A static model filter on the fact island | the fact leg | |
| A filter, or a static model filter of the fact island, that a measure's `filterContext` leaves out | the combine query, on columns the legs return | that measure's own scan leaves it out there: [grain and filter context](#grain-and-filter-context) |
| A static model filter on another island | the combine query, on a column that island's leg returns | it drops the rows it excludes and keeps an unmatched row when it holds for NULL; `enforce: always` applies even when the query reads nothing else of that island, and in the body of an `exists` subquery on that island too, as on one database |
| `exists` on its subject's island, `nonexists` on the fact island | the leg of the subject's island, with its subquery | as on one database |
| `nonexists` on a dimension island | a leg of its own returning that island's keys the subquery holds for (`exists`); the combine query keeps the fact rows whose key is not among them | it holds for a fact row with no row there (an order whose customer is missing), which a filter in the dimension leg would drop |

### Exists subqueries

An [`exists` / `nonexists`](query-language.md#existence-operators) filter runs
on the data source of its subject, with its whole subquery: the target, the
join path from the subject to it, every object its own filters and the
`enforce: always` static filters read, and the objects joined to reach them,
must be of that source. "Revenue from customers with a high-severity ticket" runs
where the customers and tickets are, and the fact leg then reads only those
customers' keys:

```yaml
where:
  - field: Customers.Customer ID     # crm
    op: exists
    subquery:
      dataObject: Tickets            # crm
      filter:
        - {field: Severity, op: equals, value: high}
```

With `op: nonexists` instead, by region, a leg of its own on the CRM returns
the keys of the customers who do have such a ticket, and the combine query
left-joins it and keeps the orders whose key found none, so an order whose
customer is missing stays, as on one database:

```sql
-- leg_2, on crm
SELECT "Customers"."customer_id" AS "__fk0_0"
FROM "customers" AS "Customers"
WHERE EXISTS (
SELECT 1
FROM "tickets" AS "Tickets"
WHERE "Customers"."customer_id" = "Tickets"."customer_id" AND "Tickets"."severity" = 'high'
)
GROUP BY ALL
ORDER BY "__fk0_0" ASC
LIMIT 5000001
```

```sql
-- combine
SELECT "leg_1"."Region" AS "Region", CAST(SUM("leg_0"."Revenue") AS DECIMAL(18, 2)) AS "Revenue"
FROM "leg_0" AS "leg_0"
LEFT JOIN "leg_1" AS "leg_1" ON "leg_0"."__fk0_0" = "leg_1"."__fk0_0"
LEFT JOIN "leg_2" AS "leg_2" ON "leg_0"."__fk0_0" = "leg_2"."__fk0_0"
WHERE "leg_2"."__fk0_0" IS NULL
GROUP BY ALL
```

A subquery may read an object the outer query joins elsewhere: the orders of
customers with a ticket handled by Ann, by each order's rep, where the tickets'
agent and the order's rep are both employees. One database joins the
employees twice, once in the outer query and once in the subquery. Federated,
so is it: the employees' leg returns the reps, and the customers' leg reads
the employees again for the subquery. A static filter on the employees runs
in their own leg; its `enforce: always` form applies in the subquery too, as
on one database.

The combine query runs no subquery, so an `exists` that would run there,
after the join, is refused: in an OR with a condition on another source, or
in a group that holds where its subject is missing.

### Key injection

When a dimension leg runs a filter, the fact leg would still aggregate every
key, and the combine query would drop the ones the filter rejected. When the
filter keeps few keys, the fact leg runs after that leg instead and reads only
them, as `key IN (...)`: the engine aggregates those rows alone, and with an
index or sort key on the column reads only those. The answer is the same.

| What | How |
|---|---|
| When | A dimension leg joined from the fact leg runs a filter of the query, joined on one column of the same type on both sides, an integer or a string. A leg further along a [chain of sources](#chains-of-sources) narrows nothing: the fact leg does not hold its keys |
| How many | At most the fact source's limit of keys, over every such leg together, the legs with the fewest keys first. A leg with more keys than are left is not listed, and the fact leg reads all of that key. No key at all reads no fact row |
| Limit | Postgres, MySQL, ClickHouse and BigQuery 10,000; DuckDB, Snowflake, Databricks and Dremio 1,000. `DB_<NAME>_KEY_INJECTION_MAX_KEYS` sets it for the fact leg's source, `DB_KEY_INJECTION_MAX_KEYS` for every source; `0` turns it off, and [key maps](#key-maps) with it |
| Cost | The fact leg waits for the dimension legs rather than running beside them. A list past the limit gains nothing over aggregating every key |
| Not listed | A list making a statement longer than the engine parses, ClickHouse's default `max_query_size` (256 KiB) or BigQuery's 1,024K characters, and a string key holding a NUL, which ends a Postgres statement's text: the fact leg reads every key instead. Snowflake refused a list of 300,000 values, where 200,000 passed |

The limits come from a measurement with 5 million orders over 1 million
customers, the filter keeping 10 to 100,000 of them:

| Fact leg on | Every key | 10 keys | 1,000 | 10,000 | 100,000 |
|---|---|---|---|---|---|
| Postgres 16 | 1.32 s | 0.10 s | 0.10 s | 0.14 s | 0.38 s |
| Postgres 16, indexed | 1.32 s (*) | 0.013 s | 0.021 s | 0.12 s | 0.39 s |
| MySQL 8.0 | 7.0 s | 0.92 s | 0.96 s | 0.96 s | 1.25 s |
| MySQL 8.0, indexed | 7.1 s | 0.017 s | 0.022 s | 0.12 s | 7.06 s (index unused) |
| ClickHouse | 0.85 s | 0.021 s | 0.045 s | 0.078 s | refused (`max_query_size`) |
| DuckDB | 0.040 s | 0.022 s | 0.034 s | 0.11 s | 0.93 s |
| BigQuery | 21 s | 1.05 s | 1.21 s | 2.20 s | 11.6 s |
| Snowflake (first time a list is sent) | 0.53 s | 0.20 s | 0.40 s | 1.42 s | 15.3 s |
| Databricks | 1.50 s | 0.33 s | 0.39 s | 0.87 s | 4.96 s |
| Dremio | 0.41 s | 0.12 s | 0.17 s | 0.44 s | 1.38 s |

Median of three runs on a laptop, the dimension leg on Postgres 16; the last
four timed on the fact leg alone, through OBSL's executor, which fetches the
million rows of every key as Arrow. Snowflake answered a list sent again under
another comment three to eight times faster, but a filter's list is new each
time, so its row shows the first. (*) For every key Postgres chose an index
scan over the indexed copy and took 11 s; the row shows the unindexed time.

!!! warning "Postgres 13 and older"
    Before Postgres 14 an `IN` list is tested one value at a time: 10,000
    keys over 5 million rows took 51 s, against 2 s without the list. Set
    `DB_<NAME>_KEY_INJECTION_MAX_KEYS=0` for such a server.

### Key maps

A fact leg that groups by more than a key, by a day or another join's key
beside it, returns a row per combination of them. When the dimension leg on
the other side of that key is small, the fact leg runs after it and joins its
keys as a map instead: each key with the number of its row among the
dimension leg's rows, written into the statement as rows of literals, and it
groups by that number where it grouped by the key. Keys whose rows hold the
same values (two products of one category) share a number. The combine step
reads one row per number for that leg and joins as before. The answer is the
same; the fact leg returns a row per group of what the query reads there:

| Revenue by category and day, products in a catalog on another source | Fact leg rows |
|---|---|
| Grouped by product and day: 10,000 products, 500 days | up to 5,000,000 |
| Joining a map of the products to 50 categories | up to 25,000 |

| What | How |
|---|---|
| When | A dimension leg joined from the fact leg on one integer column on both sides, in a query whose fact leg returns partial results grouped by more than that key; on MySQL, 8.0.19 or later. Not where the query reads the key otherwise: a dimension or a count of distinct values on its column, a `nonexists` on that source, a measure [computed from rows](#computed-from-rows) |
| How many | The [key injection](#key-injection) limit, shared: the keys of lists and maps together, the legs with the fewest keys first. A map takes the place of its key's list, as an inner join where the dimension leg runs a filter. A leg with more keys than are left is not mapped, and the fact leg groups by its key |
| Cost | The fact leg waits for the dimension leg rather than running beside it, and joining the map costs time in proportion to the fact rows: a fact leg that groups few rows can take longer |
| Not mapped | A string key: in the fact source's collation one key can equal two of the map's (`'a'` and `'A'` under MySQL's `utf8mb4_0900_ai_ci`), which would count its fact rows in both groups. A map making the statement longer than the engine parses. A dimension leg that, with the copy of its rows the map takes, would hold more than `FEDERATION_MAX_LEG_MB` in memory (a spilled leg counts its bytes on disk as held). On ClickHouse, a key declared (`sqlType`) a 128- or 256-bit integer, which has no common type with the map's `Decimal(38, 0)` keys; ClickHouse refuses a join of keys of two types without one, so declare such a key's type |

Measured with 5 million fact rows grouped by 10,000 products and 500 days,
5 million groups (the row limit), each product mapped to one of 10, 100 or
1,000 groups:

| Fact leg on | By key | Map to 10 groups | 100 | 1,000 |
|---|---|---|---|---|
| Postgres 16 | 4.48 s | 0.72 s | 1.24 s | 1.58 s |
| MySQL 8.0 | 31.2 s | 3.53 s | 3.54 s | 9.60 s |
| ClickHouse | 0.28 s | 0.24 s | 0.25 s | 0.32 s |
| DuckDB | 0.05 s | 0.13 s | 0.12 s | 0.13 s |
| BigQuery | 140 s | 1.50 s | 3.09 s | 15.3 s |
| Snowflake | 3.64 s | 0.86 s | 1.48 s | 1.40 s |
| Databricks | 20.6 s | 1.26 s | 1.73 s | 3.63 s |
| Dremio | 2.36 s | 0.84 s | 0.78 s | 0.95 s |

Median of three runs, the fact leg alone, fetched as Arrow through OBSL's
executor: Postgres, MySQL, ClickHouse and DuckDB on a laptop, the others in
their clouds and Dremio in a local container. Mapped, the leg returned 5,000,
50,000 or 500,000 rows instead of 5,000,000 rows of 155 MiB, which spill to
disk and reach `FEDERATION_MAX_LEG_ROWS`; fetching them is most of BigQuery's
time. ClickHouse and DuckDB group 5 million rows so fast that only the leg's
size changes. A smaller fact leg can take longer: MySQL grouped 73,000 product
and day groups of the 5 million rows in 2.1 s by key and in 3.4 s joining a
map. Every engine took a map of 50,000 rows but ClickHouse, which parses at
most 256 KiB of a statement: a map past that is not joined.

## Queries without measures

A query selecting dimensions alone returns the combinations the fact rows
reach, as one database does. For `Order Date:month` and `Region`, that is each
month with the region of every customer who ordered in it, and NULL for an
order whose customer is missing; no month is paired with a region its orders
do not reach.

- the fact leg returns its dimensions and the join keys, each combination once
  (grouped, with no measure);
- each dimension leg returns its keys and dimensions, as for any query;
- the combine query groups the joined rows.

Filters run where they would beside a measure, and a filter on another source
injects its keys into the fact leg. The fact leg returns each combination of
its dimensions with the join key, which can be many more rows than the result
(months by customers for months by regions); `FEDERATION_MAX_LEG_ROWS` bounds
it as any leg. Dimensions of one source alone run on that source and are not
federated. Dimensions of another source with a filter on the facts' source
are refused as on one database: the customers do not reach the orders.

Value search (`POST .../values`) runs such queries too, so a dimension's
values can be listed under a filter on another source.

## Raw mode

A [raw-mode](query-language.md#raw-mode-selectfields) query (`select.fields`)
returns rows, not groups, and is split the same way:

- the fact leg returns the fact rows, each once, with the fields of its source
  and the join keys;
- each dimension leg returns its rows with the fields read there and its keys;
- the combine query joins them as one database joins the objects, and returns
  each field under its own name, distinct, ordered and limited as asked.

The orders over 10 of EU customers, the largest first:

```sql
-- leg_0, on shop
SELECT "Orders"."order_id" AS "Orders.Order ID", "Orders"."amount" AS "Orders.Amount", "Orders"."customer_id" AS "Orders.__fk0"
FROM "orders" AS "Orders"
WHERE "Orders"."amount" > 10
ORDER BY "Orders.Order ID" ASC, "Orders.Amount" ASC, "Orders.__fk0" ASC
LIMIT 5000001

-- leg_1, on crm
SELECT "Customers"."region" AS "Customers.Region", "Customers"."customer_id" AS "Customers.__fk1"
FROM "customers" AS "Customers"
ORDER BY "Customers.Region" ASC, "Customers.__fk1" ASC
LIMIT 5000001

-- combine
SELECT "leg_0"."Orders.Order ID" AS "Orders.Order ID", "leg_0"."Orders.Amount" AS "Orders.Amount", "leg_1"."Customers.Region" AS "Customers.Region"
FROM "leg_0" AS "leg_0"
LEFT JOIN "leg_1" AS "leg_1" ON "leg_0"."Orders.__fk0" = "leg_1"."Customers.__fk1"
WHERE "leg_1"."Customers.Region" = 'EU'
ORDER BY "leg_0"."Orders.Amount" DESC
```

A filter reading the facts' source alone runs in the fact leg, an `exists`
on a subject there too; any other runs after the join, on the columns the
legs return, as one database runs it on the joined rows. Static filters run
as beside a measure. Without the model's `queryTimezone`, such a filter on a
dimension that truncates or casts a timestamp with a time zone is refused: the
leg returns the timestamp zoned, and the combine step would truncate it in its
own time zone, not the source's. No keys are injected, and the query's `limit` applies
after the join, so the fact leg returns every fact row its filters keep:
`FEDERATION_MAX_LEG_ROWS` bounds the facts a raw query can read. Fields of
independent facts, which one database unions, are refused.

## Chains of sources

A source reached through another dimension source, rather than from the
measures' source directly, is a dimension leg of its own, joined from the leg
it is reached through. For orders on the shop, their customers in the CRM, and
the customers' regions on a third source, `geo`:

```sql
-- leg_0, on shop
SELECT "Orders"."customer_id" AS "__fk0_0", SUM("Orders"."amount") AS "Revenue"
FROM "orders" AS "Orders"
GROUP BY ALL

-- leg_1, on crm: the customers' keys, and the key of their region
SELECT "Customers"."customer_id" AS "__fk0_0", "Customers"."region_id" AS "__fk1_0"
FROM "customers" AS "Customers"
GROUP BY ALL

-- leg_2, on geo
SELECT "Regions"."name" AS "Region Name", "Regions"."region_id" AS "__fk1_0"
FROM "regions" AS "Regions"
GROUP BY ALL

-- combine
SELECT "leg_2"."Region Name" AS "Region Name", CAST(SUM("leg_0"."Revenue") AS DECIMAL(18, 2)) AS "Revenue"
FROM "leg_0" AS "leg_0"
LEFT JOIN "leg_1" AS "leg_1" ON "leg_0"."__fk0_0" = "leg_1"."__fk0_0"
LEFT JOIN "leg_2" AS "leg_2" ON "leg_1"."__fk1_0" = "leg_2"."__fk1_0"
GROUP BY ALL
```

The same holds when the chain comes back to the measures' source (regions on
the shop, reached through the CRM): the regions are a leg of their own there.
Filters, `nonexists` and measures reading a column of the far source work as
for a source joined directly. Only [key injection](#key-injection) stops at
the first source: the fact leg holds the customers' keys, not the regions'.

## Measures of independent facts

Revenue of the shop's orders beside a count of the CRM's support tickets, per
region: two facts that join the customers, neither the other. One database
unions them, one leg per fact, each grouping its rows by the dimensions it
reaches (NULL for the others) under the filters it reaches, and groups the
union by all of them. Where the facts are on different islands, each is a
**part**: a query of its own with its measures, the dimensions and filters
its fact reaches, planned as any query is (federated, or on one source). The
combine query unions the parts' results and groups them by the dimensions:

```sql
-- part 0, the orders, federated: __part0_leg_0 on shop, __part0_leg_1 on crm
-- part 1, the tickets, all on crm: __part1_leg_0
SELECT "Customers"."region" AS "Region", CAST(COUNT(1) AS BIGINT) AS "Tickets Count"
FROM "tickets" AS "Tickets"
LEFT JOIN "customers" AS "Customers" ON "Tickets"."customer_id" = "Customers"."customer_id"
GROUP BY ALL

-- combine: each part's combine query over its legs, then their union
WITH "__part0" AS (...), "__part1" AS (...),
"__parts" AS (SELECT * FROM "__part0" UNION ALL BY NAME SELECT * FROM "__part1")
SELECT "__parts"."Region" AS "Region", MAX("__parts"."Revenue") AS "Revenue",
  CAST(COALESCE(MAX("__parts"."Tickets Count"), 0) AS INTEGER) AS "Tickets Count"
FROM "__parts" AS "__parts"
GROUP BY ALL
```

- A group holds each measure from one part at most. A group no row of a
  fact reaches reads a count as 0 and any other measure as NULL (or its
  `defaultValue`), as on one database.
- A filter a fact does not reach (the orders' date, for the tickets) is left
  out of its part, with the `FILTER_NOT_APPLIED` warning one database gives;
  so is a static filter, with `STATIC_FILTER_NOT_APPLIED`. Of an `and` group,
  only the conditions the fact does not reach are left out; an `or` group of
  several conditions, or a negated group, is one filter.
- A part grouping by none of the query's dimensions (the tickets, by order
  month) returns its row only where its fact has rows the filters keep: no
  tickets add no group of NULLs, as on one database.
- Metrics across the facts (revenue per ticket), `having`, `orderBy` and
  `limit` run in the combine query, after the union. A measure the query only
  orders by is computed in the part of its fact.
- The parts' legs all run at once; none is filtered by another's keys
  ([key injection](#key-injection)).

Two facts on one island (orders and returns both on the shop, joined there)
stay one fact leg, which unions them itself.

## Query options

| Option | How it runs |
|---|---|
| One dimension at two grains (`Order Date:month`, `Order Date:quarter`) | the leg returns the date once, as stored, and the combine step applies both grains |
| `fill` | the legs return the dates as stored, and the combine step fills the periods; a filter on the time dimension runs there too, so the periods span its bounds as on one database (from December, before the first order) |
| `asOf` | the combine step evaluates the cumulative metric at that date, over the [dates the legs return](#metrics-across-periods) |
| `usePathNames` | a path between two sources is the edge the combine step joins; one within a source is the leg's own join, so an order's support employee beside the orders reads the shop's employees by the support key |
| Coalesce dimensions | the legs return each member, and the combine step coalesces them |
| `dimensionsExclude` | over one group of dimensions, the pairs the joined rows hold minus themselves, none, as on one database; dimensions of two roots are refused (they are joined from two facts) |

## Role-playing dimensions

A dimension that plays a role ([`via`](model-format.md#role-playing-dimensions-via),
with a [`pathName`](model-format.md#several-roles-in-one-query-pathname)) reads
its data object through one of several joins. One database joins each role
under an alias of its own. Across sources, each role of a dimension source is
a dimension leg of its own, reading the table under that alias, and the fact
leg returns each role's key. For an order's sales and support employee, both
in the CRM:

```sql
-- leg_0, on shop
SELECT "Orders"."sales_id" AS "__fk0_0", "Orders"."support_id" AS "__fk1_0", SUM("Orders"."amount") AS "Revenue"
FROM "orders" AS "Orders"
GROUP BY ALL

-- leg_1, on crm
SELECT "Employees__Orders__sales"."name" AS "Sales Employee", "Employees__Orders__sales"."employee_id" AS "__fk0_0"
FROM "employees" AS "Employees__Orders__sales"
GROUP BY ALL

-- leg_2, on crm
SELECT "Employees__Orders__support"."name" AS "Support Employee", "Employees__Orders__support"."employee_id" AS "__fk1_0"
FROM "employees" AS "Employees__Orders__support"
GROUP BY ALL

-- combine
SELECT "leg_1"."Sales Employee" AS "Sales Employee", "leg_2"."Support Employee" AS "Support Employee", CAST(SUM("leg_0"."Revenue") AS DECIMAL(18, 2)) AS "Revenue"
FROM "leg_0" AS "leg_0"
LEFT JOIN "leg_1" AS "leg_1" ON "leg_0"."__fk0_0" = "leg_1"."__fk0_0"
LEFT JOIN "leg_2" AS "leg_2" ON "leg_0"."__fk1_0" = "leg_2"."__fk1_0"
GROUP BY ALL
```

A role whose join stays within one source (a customer's account manager,
customers and employees both in the CRM) runs in that source's leg, as on one
database. A static filter on the role's data object filters that object as
the model declares it, joined through its primary join, not a role's copy, as
on one database: with a filter on `Employees`, an order is kept by its sales
employee's team, whichever role the query selects.

## How measures combine

Most measures combine from **partial results** the fact leg computes per
group. Those that do not are computed **from the fact leg's rows**.

| Aggregation | The fact leg returns | The combine query computes |
|---|---|---|
| `sum`, `count`, synthesized counts | the sum, the count | the sum of them; a count over no rows is 0 |
| `min`, `max`, `any_value` | the minimum, maximum, any value; of strings, each one's rank among the groups' too | the minimum, maximum, any value of them; of strings, the value at the least or greatest rank ([in the source's order](#values-compared-across-engines)) |
| `avg` | its argument's sum and count | the summed sums over the summed counts, exactly |
| `count_distinct` of one column | groups by that column too | its distinct values across every group they reach |
| `variance`, `var_pop`, `stddev`, `stddev_pop` of integers or declared decimals | count, sum and sum of squares, each value squared exactly | the statistic from their totals, exactly |
| anything else | its rows | each measure, in DuckDB |

### Partial results

- **Not rounded, not bounded.** A leg leaves its sums in the engine's own type,
  which keeps the input's scale and the engine's range; the measure's type and
  `defaultValue` apply once, to the combined value.
- **`avg`** is not an average of averages. For an integer or decimal measure
  the combine query divides exactly, in integers, and rounds half away from
  zero to the measure's type, as PostgreSQL, MySQL and Snowflake round their
  own `AVG`; HAVING, ORDER BY and metrics read the average before that
  rounding, at 18 places or more. A `float` measure divides as doubles. The
  decimal sums are read at 9 places, the most an engine returns one at
  (BigQuery `NUMERIC`); a sum with more places is refused
  (`FEDERATION_LEG_TYPE`), never rounded.
- **`count_distinct`** counts are not added: the fact leg also groups by the
  counted column, which `FEDERATION_MAX_LEG_ROWS` bounds. Beside a measure of
  another data object the query is computed from rows instead.
- **Variances** combine from their moments, each value squared in a type wide
  enough to keep every digit (`DECIMAL(38, s)`, `NUMERIC`, `BIGNUMERIC` or
  `Decimal256`, by engine), added as 128-bit integers, shifted by the integer
  mean, rounded once to a double: exact, also on Snowflake and Dremio, whose
  own `STDDEV` cancels far from zero. The values must be integers or a decimal
  column declared with `sqlPrecision` and `sqlScale`, of up to 3 places on
  Databricks and Dremio, 6 on Snowflake, 15 on MySQL and 19 on the others;
  past those, the query is computed from rows. A sum of squares past 128 bits
  raises an error rather than wrapping.

### Computed from rows

When a measure does not combine from partial results (a `median`, a
percentile, `mode`, `first`, `last`, `listagg`, a correlation, covariance or
regression, a variance of floats, a `distinct` aggregation, a filtered
`count_distinct`, an `avg` or `count_distinct` with `total: true`, ...), the
fact leg returns its rows instead:

- it also groups by every column a measure reads (its argument, filters,
  ordering and expression), and counts the rows of each group, `__rows`, so
  equal rows travel once;
- the combine query repeats each group that many times (a `range()` join in its
  own SQL) and computes **every** measure of the query from those rows, as one
  database computes it from its own.

For the median order amount by region, the MySQL fact leg becomes:

```sql
SELECT `Orders`.`customer_id` AS `__fk0_0`, `Orders`.`amount` AS `__value0`,
       CAST(COUNT(1) AS SIGNED) AS `__rows`
FROM `orders` AS `Orders`
GROUP BY `Orders`.`customer_id`, `Orders`.`amount`
```

and the combine query starts with:

```sql
WITH "leg_0_rows" AS (
SELECT "__leg".* EXCLUDE ("__rows")
FROM "leg_0" AS "__leg", range(CAST(COALESCE("__leg"."__rows", 0) AS BIGINT))
)
SELECT "leg_1"."Region" AS "Region", MEDIAN(CAST("leg_0"."__value0" AS DOUBLE)) AS "Median Order"
FROM "leg_0_rows" AS "leg_0" ...
```

So an aggregation the fact leg's engine lacks, such as `corr` on MySQL, is
available across sources too. The rows a leg stands for count against
`FEDERATION_MAX_LEG_ROWS` (7 orders returned as 5 groups are 7). The leg reads
its columns in the model's `queryTimezone`, as one database does, except the
key a `first` or `last` is ordered by, which it returns as stored. Over
doubles far from zero the result depends on the order values are added in, as
on one source, and an integer variance beside a median is DuckDB's variance of
doubles rather than the exact one from moments.

### Measures reading another source

A measure of the fact's source may read a column of another one: a line's
quantity times a product's price kept in the catalog, revenue filtered on the
customer's segment in the CRM, the first order by the customer's signup date.
Such a measure is computed from rows, after the join:

- the dimension leg of the other source returns each column the measure reads
  beside its keys, read there as one database reads it (a `first` or `last`
  ordering key as stored, any other column in the `queryTimezone`);
- the fact leg returns its rows, as above, and the combine query joins the two
  and computes the measure over the joined rows, as one database does.

For revenue as quantity times price, by batch, the catalog leg becomes:

```sql
SELECT "Products"."product_id" AS "__fk0_0", "Products"."price" AS "__value1"
FROM "products" AS "Products"
GROUP BY ALL
ORDER BY "__fk0_0" ASC, "__value1" ASC
LIMIT 5000001
```

and the combine query computes
`SUM("leg_0"."__value0" * "leg_1"."__value1")` over the fact rows left-joined
to it. A filter on the other source still runs in its leg, and its keys are
injected into the fact leg as for any filter there.

Its values are compared as DuckDB compares them, so a value its own engine
compares otherwise (a MySQL string in the measure's filter, say) is refused.
A filter on such a value of the fact's own source runs in the fact leg
([values compared across engines](#values-compared-across-engines)). A measure counted once per row of
another object of the fact's source (a customer's credit beside their orders)
cannot read another source yet, nor can a measure read a column computed from
columns of two sources.

### Metrics across periods

[Cumulative, period-over-period and window metrics](model-format.md#metrics)
run in the combine step, over the combined values, as one database runs them
over its own: a running total, a rolling window, grain-to-date, a rolling
time span, a change against an earlier period, a rank, a lag.

- The legs return each date column as stored, an order at its own date, and
  the combine step applies the grains: the one a dimension declares
  (`timeGrain`) and the one the query selects (`Order Date:month`). A rolling
  window over months then counts months, and a time span reads each order's
  date. Every dimension over one date column (`Order Date`, `Order Month`,
  whatever result type it declares) reads that one column there, and the
  metric's time dimension is returned even when the query does not select it:
  a running total by region alone sums up to the last order date.
- A filter on the metric's time dimension, or on another dimension over its
  date column, runs in the combine step. It says which periods to show, not
  which the metric reads: shown from February, February's change is against
  January, and March's running total counts January, as on one database.
- The fact leg groups by each date, finer than the query: a measure of an
  object the joins repeat is [computed from rows](#objects-the-joins-repeat),
  and so is a measure over a time span that does not add up from partial
  results (an average, a variance, a median, a count of distinct values).

A metric whose time dimension is on a data object the query does not join,
a calendar it neither selects nor filters on, is refused: one database joins
it in a query of the metric's own, so it limits no other measure, where a
leg joining it would limit them all. So is a date dimension over a timestamp
with a time zone that the combine step would truncate, or cast to a date,
beside such a metric: it would do so in its own time zone, and an order of Los
Angeles' New Year's Eve would move into January.

### Reaggregate metrics

A [reaggregate metric](model-format.md#reaggregate-metrics) runs in the
combine step, both stages, as one database runs them: the first stage, its
measure grouped by the query's dimensions and its `per` dimensions, is a query
of its own over the legs' results, and the second aggregates those values again
to the query's grain.

- The legs return the `per` dimensions beside the query's. One the query does
  not join is joined as one database's first stage joins it, many-to-one from
  the base object: revenue per customer by order month reads the CRM's
  customers, where the query alone reads only the shop.
- A `per` date is returned as stored, and the combine step applies its grain
  and the query's: the worst month of each year. As beside a metric across
  periods, a timestamp with a time zone is then refused at a grain.
- With `carryForward`, the dates are returned as stored too, and a filter on
  the query's dates runs in the combine step: a customer without orders in
  February carries January's value into it, though the query starts in
  February.
- The metric's `having`, `population: dimension`, and a metric over it
  (derived, cumulative, window, or another reaggregate metric) work as on one
  database, and so does a query ordering by it alone. A required join in a
  source only the first stage reads limits that stage alone: the combine step
  joins that source's leg only where a stage reads it.

Refused, where the legs cannot return a `per` dimension as one database's
first stage groups by it:

- one no many-to-one join reaches from the rows of the metric's measure: one
  database's first stage reads the customers' count per order date from the
  orders, which the customers' rows do not join;
- one reached by a required join the query does not make, anywhere on the
  way, in a leg the query or another reaggregate metric's first stage reads
  too (the measure's own source, or one either joins): that leg would drop
  their rows as well;
- with `population: dimension`, one whose source's leg reads anything else:
  the fact leg holds only the members with rows, and a leg reaching them
  through another object only those its rows reach. The `per` dimensions'
  data objects have to be alone on their source in the query.

### Objects the joins repeat

A measure of an object the query's joins repeat (customers' ages over their
sales) counts each of its rows once per group, as one database does. Partial
results do too when everything the fact leg groups by beyond the query is read
from that object or from one it joins many-to-one. Otherwise (a join to
another source on the sales side, a filter after the join, a subtotal or total
across such a dimension) the query is computed from rows:

- the leg also returns each such object's identity (its `primaryKey`, else the
  key its join reaches it by) and counts its rows, so it returns the customers
  without a sale as well, as one database adds them;
- the combine model reads each such object's rows once each, as a relation of
  its own, with what its measures read and the dimensions and keys the query
  reads of it, or the outcome of a condition on them its leg evaluates
  ([Values compared across engines](#values-compared-across-engines)), and
  counts each once per group: a customer without sales is in the group of its
  own region;
- a filter of such a measure on the sales themselves (the ages of the
  customers with a sale of one product) reads the fact rows, as one database
  filters the joined rows before it counts each customer once: the relation
  of the customers' rows holds none of the sales' columns.

Still refused: such a measure reading a sales column other than in a filter
(as its argument, expression or ordering), or a column of its own object
computed from the sales, which one database refuses as well; and a dimension
or key read through two such objects at once, one joined from the other (the
accounts' credits beside their owners' ages, by the owners' region), where
each measure adds the rows of its own object no sale reaches.

### Grain and filter context

A measure with a [`grain` or `filterContext`](grain-filter-context.md) is
computed in the combine step, as one database computes it: at its own grain,
over its own filters, and joined back to the query's rows on the grain's
dimensions.

- A `grain` adds up groups across the query's other dimensions, as
  `total: true` does. An `avg`, a variance or a `count_distinct` with one is
  computed from rows, and so is a measure of an object the joins repeat.
- A filter that some measure's `filterContext` leaves out (`mode: FIXED`,
  `exclude`, or a `keepOnly` that does not name it) runs in the combine step.
  The legs return their groups before it, so that measure's scan can leave it
  out. The same goes for a static filter of the fact island; one with
  `enforce: always` is never left out. A filter every context keeps still runs
  in its leg. The filters that move make the legs return more groups, bounded
  by `FEDERATION_MAX_LEG_ROWS`, and a measure of an object the joins repeat
  is then computed from rows, as with a filter after the join: the fact leg
  groups by what the moved filters read.
- A measure of an object the joins repeat counts each of its rows once, with
  or without a filter context, as one database's scan of its own does.
- The legs also return the dimensions that an `include` filter reads, and
  those are checked as a filter's are: one computed with a value where its
  source's row is missing is refused.

## Values compared across engines

The combine step groups and counts values in DuckDB, which compares strings
bytewise and makes one value of `0.0` and `-0.0`. Two engines differ, and their
legs return what DuckDB then compares as they do:

| Source | Its equality | What the leg returns |
|---|---|---|
| MySQL | a string by its column's collation: the default `utf8mb4_0900_ai_ci` makes one value of `'a'` and `'A'`, `'e'` and `'é'`, `'ss'` and `'ß'` | each selected string dimension, and each string a `count_distinct` counts, as one member of its class, the same for all of them: `FIRST_VALUE(x) OVER (PARTITION BY x ORDER BY CAST(x AS BINARY))`. A group shows that member |
| ClickHouse | a float by its bits: `0.0` and `-0.0` are two values | a float a `count_distinct` counts, as its text (`toString`) |

The other six engines compare as DuckDB does, measured. What still compares as
DuckDB does is listed under [limitations](#results-that-can-differ-from-one-database).

A join between two legs on MySQL matches its string keys by their collation
too. Both legs return each key as its weight string, in hex
(`HEX(WEIGHT_STRING(x))`, weighed without its trailing spaces where the
collation pads them, as `utf8mb4_general_ci` does), which is the same
whichever leg computes it: an order's `'a1'` finds the product stored as
`'A1'`, as on one source. Measured against MySQL's `=` under twelve
collations, they match alike but for a trailing ideographic space, which
`utf8mb4_unicode_ci` pads as a space. Where the fact leg reads only the keys
a filtered dimension leg returned ([key injection](#key-injection)), it reads
them as stored, and MySQL compares them by its collation. Keys between legs
on two engines compare bytewise: neither's equality is one database's.

Order is another matter: a source orders strings by its collation, which
on PostgreSQL's `en_US.utf8`, the default of many installations, and on
MySQL's `utf8mb4_0900_ai_ci` puts `'b'` before `'C'`, where DuckDB, ordering
bytes, puts it after. A `min` or `max` of strings combines in its source's
order: the fact leg ranks each group's value among the groups', in its own
engine (`DENSE_RANK() OVER (ORDER BY __leg."Least Status")`), and the
combine step takes the value at the least or greatest rank, with totals,
grain overrides and `rollup` / `cube` subtotals too. Of values the engine
calls equal, which share a rank, it takes the bytewise least or greatest,
one of those one database may answer. What else orders strings in the
combine step orders them bytewise, as [listed](#results-that-can-differ-from-one-database).

A measure [computed from rows](#objects-the-joins-repeat) whose filter compares
such a value, a MySQL string, has its filters evaluated by the fact leg, in
its own engine: the leg returns `1` where they hold, else `0`, beside the rows
(`CASE WHEN __value1 = 'paid' THEN 1 ELSE 0 END AS __mf0`), and the combine
step reads that column. A median of the paid orders then counts `'Paid'` and
`'PAID'` as MySQL does. The filters may read only columns of the fact's own
source, neither computed nor dates, which the leg returns as they are stored,
and compare a string with strings (`equals`, `gt`, `inlist`, `between`,
`set`, ...): the leg evaluates them over its groups, one per value its engine
calls equal, and a pattern (`like`, `contains`, `starts_with`, `ends_with`,
...) can tell such values apart, `'a'` from `'a '` under
`utf8mb4_general_ci`, and so can a comparison with a number, `'1'` from `'１'`
under `utf8mb4_0900_ai_ci`. Otherwise the measure is refused.

A condition after the join (in `where` beside a condition on another source,
true for NULL, or left out by a measure's `filterContext`, in a
`filterContext`'s `include`, or a static filter on another source than the
measures') that compares such a value runs in that value's leg too. The leg
returns `1` where it holds, `0` where it fails and NULL where it is unknown
(`CASE WHEN __leg.Region = 'eu' THEN 1 WHEN NOT __leg.Region = 'eu' THEN 0 END
AS __wf0`), and the combine step tests that column, so `Region = 'eu' OR
Amount > 90` keeps the orders of the region MySQL stores as `'EU'`, and a
negated group stays unknown where MySQL's does. A row the join leaves
unmatched reads NULL there, where a comparison is unknown too. A `blank` test
holds for NULL and a `notblank` one fails, never unknown: for those the leg
returns `1` where the condition fails (`blank`) or holds (`notblank`), NULL
otherwise, and the combine step tests `IS NULL` or `IS NOT NULL`, so an order
whose customer is missing passes `Region blank`, as in one database. A filter
context leaving out or keeping only the field names that column with it. The
leg of a dimension source returns one value per key, and evaluates any
condition; the fact leg returns its rows grouped, one value for all those its
engine calls equal, and evaluates only a comparison with strings, as it does a
measure's filters. A pattern, or a comparison with a number, on a MySQL string
of the measures' own source stays in the combine step, and compares as DuckDB
does.

## Timestamps without a time zone

A timestamp without a time zone (`abstractType: timestamp`) is a wall-clock
time, in the zone its source's session runs in, and two sources may run in
two: a shop's PostgreSQL in UTC, a CRM's DuckDB on a server in Berlin. The
legs read such timestamps in one zone, the query's:

| Zone | Which |
|---|---|
| The query's | the model's `queryTimezone`, else its `defaultTimezone`, else the OBSL server's (`TZ`, else the zone of its system clock), else UTC |
| A source's | `DB_<NAME>_TIMEZONE`, else `DB_TIMEZONE`, else the zone its session reports, [detected](model-format.md#resolution-order) once per source; BigQuery's is UTC, and Databricks' and Dremio's are not detected |

A leg whose source runs in another zone converts each such timestamp at its
column, before a grain, a filter or an aggregate reads it (`"placed_at" AT
TIME ZONE 'UTC' AT TIME ZONE 'Europe/Berlin'`), as `queryTimezone` does on
one database; a leg in the query's zone returns them as stored. An order
placed at 23:30 UTC on 31 January is February's in Berlin, in its leg and
after the join alike, and `Placed At >= 2024-02-01` keeps it. The result is
labeled with the query's zone (the response's `timezone`): its values are the
instants each source holds, as wall-clock times there. A join key between
sources is compared as stored, as one database holding both tables compares
it, and a converted value read through an [unnest](#arrays-of-another-source) is refused
where one read in `queryTimezone` is.

A model that says which zone its timestamps were written in, its
`defaultTimezone` beside a `queryTimezone`, or with
`overrideDatabaseTimezone`, has every leg read them so, whatever zone a
session runs in: from its `defaultTimezone` into its `queryTimezone`, or as
stored. A leg whose source's zone is not known returns its timestamps as
stored, and the query warns `UNDECLARED_TIMESTAMP_ZONE`, naming the source;
`DB_<NAME>_TIMEZONE` declares its zone. A value a driver hands over with a
zone, Databricks' `TIMESTAMP` or a ClickHouse `DateTime` read in one, is its
wall-clock time in that zone. A ClickHouse `DateTime` holds an instant, which
its leg converts as stored, whatever zone is declared.

## Running a federated query

Every surface that executes queries runs a federated one the same way: REST
`query/execute`, the one-shot batch (`oneshot/batch`), OBSQL (REST
`query/semantic-ql`, Flight SQL and pgwire) and `obsl execute`. `query/sql` and
`obsl compile` show the plan, the REST execute and one-shot responses list the
legs in `federation`, lineage names each table's data source, and the database
`EXPLAIN` of `query/plan` explains each leg on its own source
(`database_explain_legs`); the combine query runs in OBSL, so no warehouse has
a plan for it.

| What | How |
|---|---|
| Caching | The result is cached like any query's. Its key covers the combine query and every leg's SQL with the data source it runs on, its TTL is the shortest of all the tables the legs read, and a change to any of them invalidates it; a hit runs nothing. A result computed from legs served from the cache expires no later than the first of them, so it is never older than its legs. Each leg is also cached on its own, keyed by its data source and SQL, so a query that changes only what the combine step does (a filter after the join, HAVING, ORDER BY, LIMIT) reuses the legs an earlier query fetched, and one that changes a filter running in a dimension leg reruns that leg, and the fact leg too when it reads that leg's keys |
| Legs | Run at once, each on its own source; a fact leg reading [injected keys](#key-injection) or joining a [key map](#key-maps) runs after the dimension legs it reads them from, and the other legs run beside it |
| Types | Each leg's columns are made exact for DuckDB before combining: Postgres `numeric`, which arrives as text, becomes a decimal at the scale its values carry, and a decimal wider than 38 digits narrows when its values fit. A value no decimal holds (`NaN`, more than 38 digits) is refused with `FEDERATION_LEG_TYPE`, never rounded |
| Errors | A leg that fails fails the query, and the error names the leg's data source and dialect (HTTP 502) |
| Leg size | A leg may return at most `FEDERATION_MAX_LEG_ROWS` rows (default 5,000,000), and a leg returning rows may stand for at most as many. A leg held in memory may also take at most `FEDERATION_MAX_LEG_MB` (default 512), counted batch by batch as it arrives, so a leg too large is refused before it is whole: rows alone do not say what a leg takes. Measured, a key and two partial results take 32 bytes a row (154 MB for 5,000,000 rows), a key and a region 15; each text field takes its length plus 4. Past either limit, `FEDERATION_LEG_TOO_LARGE`, naming the joins whose keys the leg returns a row for each of ([where to join the sources](#where-to-join-the-sources)). A leg spilled to disk is bounded by the spill limits below instead, except for the columns the combine step needs converted (a PostgreSQL `numeric` arrives as text and becomes a decimal), which it holds in memory within `FEDERATION_MAX_LEG_MB`, counted before each column is converted. Held in memory, narrow legs take about 75 MB of the process per million rows: measured, revenue by region over 5,000,000 customers (a fact leg from PostgreSQL and a dimension leg of 5,000,000 rows each) took 730 MB and 6 s, and four such queries at once 2.7 GB. A server with less memory lowers `FEDERATION_MAX_LEG_MB` or `FEDERATION_MAX_CONCURRENT`, or spills |
| Concurrency | At most `FEDERATION_MAX_CONCURRENT` federated queries run at once per process (default 4). The others wait in order of arrival for up to `FEDERATION_QUEUE_TIMEOUT_SECONDS` (default 30), then get `FEDERATION_BUSY` (HTTP 503). Legs run on a pool of `FEDERATION_LEG_THREADS` threads (default 16) |
| Combine | A fresh in-memory DuckDB per query, with `FEDERATION_COMBINE_THREADS` threads (default 2) and `FEDERATION_COMBINE_MEMORY_LIMIT` (default `512MB`) instead of DuckDB's defaults of every core and 80% of RAM per instance |
| Spill to disk | On by default: into `FEDERATION_SPILL_DIR`, else the system's temp directory (`TMPDIR`, else `/tmp` or the platform's), unless that is in memory, a tmpfs or ramfs on Linux or Cloud Run's filesystem, where nothing spills unless `FEDERATION_SPILL_DIR` names a directory. `FEDERATION_SPILL_DIR=off` turns it off. A leg is held in memory up to `FEDERATION_SPILL_THRESHOLD_MB` (default 64); past that, it goes to an Arrow IPC file that the combine step reads memory-mapped, and DuckDB spills its own join and sort state next to it. Each query writes into its own directory, readable by the server alone and removed when the query ends, also when it fails. One query may spill at most `FEDERATION_SPILL_MAX_MB` (default 4096, else `FEDERATION_SPILL_TOO_LARGE`, HTTP 502), and the queries running at once at most `FEDERATION_SPILL_BUDGET_MB` together (default 16384, else `FEDERATION_SPILL_FULL`, HTTP 503). A spilled leg is not stored in the result cache; the combined result is |

Spilling trades time for memory, for legs past the threshold only. Measured
with legs of 11,000,000 rows, one query took 5.6 s instead of 5.0 and four at
once a median of 9.5 s instead of 6.0, holding 375 MB of memory the operating system
cannot reclaim instead of 665, and 639 MB instead of 1,915.

Spilling needs real disk. On an in-memory filesystem, such as Cloud Run's
default one, a spilled file takes the same memory it was meant to free, so by
default nothing spills there. A `FEDERATION_SPILL_DIR` given is used as
given: point it at a disk-backed volume.
[Docker](../getting-started/docker.md#spilling-federated-queries-to-disk)
shows the volume to mount, how large it needs to be, and the same for
Kubernetes.

## Limitations

A federated query returns the answer one database holding every table would
return, or it is refused with an error that says why. Most refusals come when
the query is compiled, before any source is read; a few come
[while it runs](#when-the-query-runs). The error names the dimension, measure
or join it is about, and its hint says what to ask instead.

The examples below use the shop's orders and the CRM's customers of
[the example](#an-example). Find your query by what it does:

| Your query | Refused when | Section |
|---|---|---|
| Reads measures of two sources | a measure of the customers is beside the orders' revenue; facts that stand apart are combined with a subtotal, a total, a metric across periods, `fill` or a few other options | [Measures from several sources](#measures-from-several-sources) |
| Joins the sources | measures are grouped by the many side of their join; a join repeats rows, many-to-many between the sources or inside the other one | [Joins between sources](#joins-between-sources) |
| Reads an array of another source | an unnested value (a customer's tags) is read without grouping by it, or beside a subtotal | [Arrays of another source](#arrays-of-another-source) |
| Reads computed columns | a column is computed from another source, or has a value where the join finds no row (`COALESCE`) | [Computed columns](#computed-columns) |
| Computes a measure from rows | it compares MySQL strings or ClickHouse floats, which DuckDB compares otherwise | [Measures computed from rows](#measures-computed-from-rows) |
| Uses a metric across periods or a reaggregate metric | its dates are on an object the query does not join; a date with a time zone is grouped by a period | [Metrics](#metrics) |
| Filters | on a column rather than a dimension; with `exists` across sources | [Filters](#filters) |
| Runs | a source returns too many rows, or too many queries run at once | [When the query runs](#when-the-query-runs) |
| Is compared with one database's answer | nothing is refused: string and NULL order, time zones, no snapshot across sources | [Results that can differ](#results-that-can-differ-from-one-database) |

### Measures from several sources

Measures of the orders may read the customers: revenue of the EU customers'
orders is [computed from rows](#measures-reading-another-source). Measures of
facts that stand apart, orders and tickets that both join the customers, are
[combined side by side](#measures-of-independent-facts).

| You asked for | Ask instead | Error |
|---|---|---|
| A measure of the customers beside the orders' revenue, a count of the CRM's customers per region. Also when the query only orders by such a measure | Query each source's measures apart. A distinct count of the orders' customer key, on the shop, counts the customers that have orders | `FEDERATION_MULTI_FACT` |
| Facts that stand apart, on different sources, beside any of: a measure with `total`, `grain` or `filterContext`; a measure reading several of the facts, or anchored to another; `rollup` or `cube`; a cumulative, period-over-period, window or reaggregate metric; `fill`, `asOf`, or one dimension at two grains; `dimensionsExclude` or a coalesce dimension | Query each fact's measures apart, or leave out the option | `FEDERATION_UNSUPPORTED_QUERY` |
| Facts that stand apart, ordered by a measure of a fact none of the selected measures is of (revenue per region, ordered by the tickets' count) | Select that measure too | `FEDERATION_UNSUPPORTED_QUERY` |
| Facts that stand apart, in raw mode (`select.fields`) | Select the fields of one fact and the objects it joins, one query per fact; or select dimensions and measures, which combine them | `FEDERATION_UNSUPPORTED_QUERY` |

### Joins between sources

Across sources, each fact row joins one row of the other source, as a
many-to-one (or one-to-one) join does on one database.

| You asked for | Ask instead | Error |
|---|---|---|
| Measures grouped by a dimension on the many side of their join: a count of the CRM's customers by the orders' amount, where a customer has many orders | Group by dimensions of the measures' own source, or of objects they join many-to-one. A distinct count of the orders' customer key, on the shop, counts the customers per amount | `FEDERATION_UNSUPPORTED_JOIN` |
| A many-to-many join between sources, or a many-to-one join walked from its one side: the topics the CRM's customers link to, kept on another source. One database counts an order once per topic, where across sources each order joins one row | Group by dimensions joined many-to-one from the measures' side, or query it on one data source | `FEDERATION_UNSUPPORTED_JOIN` |
| Inside the other source, a join that repeats rows: a many-to-many join, or a many-to-one join walked from its one side. An order's customer, and the topics the customer links to: one database counts the order once per topic, where the CRM's leg returns each customer once | Group by dimensions the other source reaches many-to-one from the object the join enters, or query it on one data source | `FEDERATION_UNSUPPORTED_JOIN` |
| Objects of another source that the measures' source reaches by two joins at once, or by none | Query it on one data source | `FEDERATION_UNSUPPORTED_JOIN` |

### Arrays of another source

A [nested object](model-format.md#nested-data-objects-nestedin) (`nestedIn`)
unnests an array, a customer's tags. On another source than the measures',
its leg returns a row per tag, and the orders join each. Grouped by the tag,
or filtered on it alone, an order counts once per group, as on one database.
Anything that would count an order once per tag of its customer is refused,
also for what is read through the tags (a tier joined to a tag, a customer
column computed from them).

| You asked for | Ask instead | Error |
|---|---|---|
| A tag dimension beside a `rollup` or `cube` subtotal, a measure's `total`, or a measure's `grain`, which add the tags' groups up | Select it without the subtotal, total or grain, or query it on one data source | `FEDERATION_UNSUPPORTED_QUERY` |
| A tag the query reads without grouping by it: in a measure's filter, a static filter, a filter beside a condition on another source (in an `or`, or one a measure's `filterContext` leaves out), a join on to a third source, a reaggregate metric's `per`, or beside a metric across periods. Grouping by a coarser value does not count: by month, for a measure reading the tag's date; by a value shown in the `queryTimezone`, or computed from one (its hour), for the stored timestamp | Select what it reads as a dimension too, as stored (the tag's date, not its month), or query it on one data source | `FEDERATION_UNSUPPORTED_QUERY` |
| An `exists` on a customer whose subquery reads the tags: the orders of customers tagged `'vip'` | Filter on the tag dimension instead, `Tag = 'vip'`: the same orders | `FEDERATION_UNSUPPORTED_JOIN` |

### Computed columns

A column computed from other columns is computed in its own source's leg,
from the objects that leg reads.

| You asked for | Ask instead | Error |
|---|---|---|
| A column of a left-joined source with a value where the row is missing: `COALESCE({[Customers].[Region]}, 'unknown')` for an order whose customer is not in the CRM. One database computes `'unknown'`, the combine step would read NULL. As a dimension, a field, a filter, a measure's input, a static filter or a join key | Compute it so it stays NULL where its inputs are (`UPPER`, arithmetic), and label the missing rows in your tool; or make the join `required`, which keeps only the orders whose customer is found | `FEDERATION_UNSUPPORTED_JOIN` |
| A dimension, a field or a static filter computed from an object the query does not join to the column's own within its source: the customers' home region, computed from the regions, with the regions on another source, or joined to the orders directly | Compute it from objects of its own source that its object joins, or query it on one data source | `FEDERATION_UNSUPPORTED_JOIN` |
| A measure reading a column computed from columns of another source | Compute the column from columns of the measure's own source | `FEDERATION_MEASURE_ACROSS_SOURCES` |
| A measure of an object the facts' rows reach ([counted once per row](#objects-the-joins-repeat), the customers' ages in a query of their sales) that reads a column of another source | Query that measure on one data source | `FEDERATION_MEASURE_ACROSS_SOURCES` |

### Measures computed from rows

A measure that does not add up from partial results (a median, a
percentile, `mode`), or that reads a column of another source, is
[computed from rows](#computed-from-rows) in the combine step's DuckDB.

| You asked for | Ask instead | Error |
|---|---|---|
| Such a measure over values its engine compares otherwise than DuckDB. A MySQL string, whose collation makes `'a'` and `'A'` equal: in a filter that also reads another source, a computed column or a date; as the ordering or expression; or as the argument of `min`, `max`, a percentile, `mode`, `first`, `last`, `listagg` or a `distinct` aggregation. A ClickHouse float it counts (`mode`, a `distinct` aggregation), where `0.0` and `-0.0` are two | Query it on one data source. A `min` or `max` of strings is refused only beside a measure computed from rows: query it apart. A filter comparing only plain columns of the measures' source (strings with strings, without a pattern) runs in its leg and is not refused | `FEDERATION_UNSUPPORTED_AGGREGATION` |
| A measure of an object the facts' rows reach, the customers' ages in a query of sales, reading a sales column other than in a filter (as its argument, expression or ordering): one customer has several | Query it on one data source | `FEDERATION_UNSUPPORTED_AGGREGATION` |
| A dimension or key read through two such objects at once, one joined from the other: the accounts' credits beside their owners' ages, by the owners' region | Query the measures of each object apart | `FEDERATION_UNSUPPORTED_AGGREGATION` |

### Metrics

| You asked for | Ask instead | Error |
|---|---|---|
| A cumulative, period-over-period or window metric whose time dimension is on an object the query does not join: a running total by region alone, over a calendar it neither selects nor filters on | Select its time dimension, or query the metric on one data source | `FEDERATION_UNSUPPORTED_METRIC` |
| A reaggregate metric whose `per` dimension the measure's rows do not reach many-to-one (the customers' count per order date); or whose `per` dimension a required join reaches that the query does not make, in a leg the query or another reaggregate metric reads too; or, with `population: dimension`, whose source's leg reads another object too | Query it on one data source | `FEDERATION_UNSUPPORTED_METRIC` |
| A dimension over a timestamp with a time zone, at a grain or as a date, beside a cumulative, period-over-period or window metric, or a reaggregate metric per a date. Without the metric, with `fill` or at two grains in one query: the combine step would group it in its own time zone, not its source's | Select it as a timestamp with a time zone, without a grain | `FEDERATION_UNSUPPORTED_METRIC`, `FEDERATION_UNSUPPORTED_QUERY` |

### Filters

| You asked for | Ask instead | Error |
|---|---|---|
| A filter on a data object's column, `Customers.Region`, rather than a dimension; also as a `filterContext` `include`. Raw mode takes such a filter | Declare a dimension over the column and filter on that | `FEDERATION_UNSUPPORTED_FILTER` |
| An `exists` or `nonexists` whose subquery reads another source than its subject: its target, the join path to it, or an object its filters read | Keep the subquery on its subject's source | `FEDERATION_UNSUPPORTED_FILTER` |
| An `exists` or `nonexists` in an `or` with a condition on another source, or in a group that is true for an order without a customer | Filter with it on its own, or beside conditions of its subject's source only | `FEDERATION_UNSUPPORTED_FILTER` |
| An `exists` or `nonexists` that a measure's `filterContext` leaves out | Keep it in every measure's `filterContext` | `FEDERATION_UNSUPPORTED_FILTER` |
| In raw mode, an `exists` or `nonexists` on a subject of another source than the facts' | Select dimensions and measures, or keep it on the facts' source | `FEDERATION_UNSUPPORTED_FILTER` |
| In raw mode, without the model's `queryTimezone`, a filter after the join on a dimension that truncates or casts a timestamp with a time zone (its month, its date) | Set the model's `queryTimezone`, or query it on one data source | `FEDERATION_UNSUPPORTED_FILTER` |

An `exists` over an array of another source is listed under
[Arrays of another source](#arrays-of-another-source).

### When the query runs

| What happened | What to do | Error |
|---|---|---|
| A leg returned more than `FEDERATION_MAX_LEG_ROWS` rows (5,000,000), or held more than `FEDERATION_MAX_LEG_MB` (512) in memory. Mostly a join on a key with many values: revenue by region, with the customers on another source, returns a row per customer from the shop ([where to join the sources](#where-to-join-the-sources)). The error names those joins | Filter on the other source's dimensions, which narrows both legs to the keys it returns ([key injection](#key-injection)); join on a column with fewer values (a region rather than a customer); or raise the limit | `FEDERATION_LEG_TOO_LARGE` (HTTP 502) |
| A leg returned a value no decimal holds (PostgreSQL's `NaN`, more than 38 digits), or the partial sums of an `avg` as floats or with more places than the combine step adds exactly | Filter the value out; for an average, declare the measure's `resultType` float, or average a fixed-point column | `FEDERATION_LEG_TYPE` (HTTP 502) |
| More federated queries than `FEDERATION_MAX_CONCURRENT` (4) ran at once, and this one waited past `FEDERATION_QUEUE_TIMEOUT_SECONDS` | Retry, or raise the limit where the process has memory to spare | `FEDERATION_BUSY` (HTTP 503) |
| A query spilled more than `FEDERATION_SPILL_MAX_MB` to disk, or all queries together more than `FEDERATION_SPILL_BUDGET_MB` | Filter the query; retry later; or raise the limits and the disk | `FEDERATION_SPILL_TOO_LARGE` (502), `FEDERATION_SPILL_FULL` (503) |

### Results that can differ from one database

Not refused, and listed so they are known. Most have a setting that makes
them agree.

| What you may see | Why | What to do |
|---|---|---|
| A NULL group sorted last where one database sorts it first | ordered after the join, NULLs sort as DuckDB sorts them: last, ascending and descending. PostgreSQL puts them first in a descending order, MySQL first in an ascending one | set `nulls: first` or `nulls: last` on the `orderBy` |
| Strings in another order | ordered after the join, strings sort bytewise: `'C'` before `'b'`, where PostgreSQL's `en_US.utf8` or MySQL's collation puts `'b'` first. So do a `having`, a window metric, a reaggregate metric's own `min` or `max`, a filter after the join comparing strings by order (`gt`, `between`, ...; on MySQL it [runs in its leg](#values-compared-across-engines)), and a measure [computed from rows](#computed-from-rows). A `min` or `max` of strings otherwise combines in its source's order | order by a value with one order everywhere (a code, a number), or sort in your tool |
| Rows read at slightly different moments | there is no snapshot across sources: each leg reads its source when it runs | read sources that change during the query at a quiet time |
| Timestamps without a time zone off by the source's offset | from a source whose session zone is not known (Databricks, Dremio, a MySQL session in its server's zone, `SYSTEM`, where that is not UTC), they are returned as stored, where the other legs [convert theirs](#timestamps-without-a-time-zone) to the query's zone. The query warns with `UNDECLARED_TIMESTAMP_ZONE` | set `DB_<NAME>_TIMEZONE` for the source |
| Timestamp join keys that do not match | keys are compared as stored, as one database holding both tables compares them: a key stored in UTC on one source and in New York time on another does not match its instant. A key with a time zone is read in the zone of the OBSL server process | store the keys in one zone, or join on another key |
| Today's date off by one around midnight | a `relative` filter counts from the date of the engine that runs it: a leg's source in its session's zone; after the join, the OBSL server process's zone | run the OBSL server in the sources' zone, or filter on fixed dates |
| A timestamp with a time zone compared with a date literal (`'2023-12-01'`) after the join | the combine step reads the literal in the OBSL server process's zone, a source reads it in its session's | set the model's `queryTimezone`: the legs return wall-clock times, which compare alike |
| One group for the hour the clocks go back, on ClickHouse | legs [read timestamps](#timestamps-without-a-time-zone) as wall-clock times in the query's zone, where 02:30 in Berlin is two instants an hour apart: the combine step groups them as one, as the other engines do. ClickHouse alone keeps them apart | use a `queryTimezone` without daylight saving time, UTC |
| `'a'` and `'A'` as two rows in raw mode | raw-mode `distinct` deduplicates as DuckDB: two MySQL strings its collation calls equal are two rows | select them as dimensions, which group as MySQL does |
| A pattern after the join that MySQL would match | a pattern (`like`, `contains`, ...) or a comparison with a number, on a MySQL string of the measures' source, in a filter after the join (in an `or` with another source's condition), compares bytewise. [Other conditions](#values-compared-across-engines) run in their leg | keep the pattern out of an `or` with conditions of other sources |
| Join keys that do not match by collation | between legs on two engines, keys compare bytewise, whatever a source's collation. Between two MySQL legs they compare [by their weight strings](#values-compared-across-engines), which match where one MySQL database would compare by the binary collation or refuse | join on keys with the same bytes on both sides (IDs, normalised codes) |
| `0.0` and `-0.0` as one group, from ClickHouse | a ClickHouse float dimension groups in DuckDB, which calls them equal | none |
| Each order once per tag of another source, where one database refuses | grouped by a tag of another source, the orders count once per tag. One database does the same through the orders' `primaryKey`, and refuses the query without one | declare the orders' `primaryKey` for one database to agree |
| The last digits of a double | a measure of doubles computed from rows adds them in the order they arrive, as one source does: far from zero the result depends on it | use a decimal column for exact sums |
| A variance of integers as a double | beside a measure that needs rows, a variance of integers is DuckDB's double variance, not the exact one from its moments | query the variance apart |
| An overflow on a product of decimals | computed from rows, two decimals of 18 digits or fewer multiply to `DECIMAL(18, s1 + s2)`, so two of 2 places overflow past 10^14, where PostgreSQL and MySQL compute it exactly | none yet |
| More rows read than the filter needs | the fact leg reads every key where [key injection](#key-injection) is skipped: a key of two columns, of a type other than integer or string, or past the limit. The answer is the same | for a key past the limit, raise `DB_<NAME>_KEY_INJECTION_MAX_KEYS` |
| A larger leg than the filter needs | the fact leg groups by the key where a [key map](#key-maps) is skipped: a key of two columns, a key other than an integer, or past the limit. The answer is the same | for a key past the limit, raise `DB_<NAME>_KEY_INJECTION_MAX_KEYS` |

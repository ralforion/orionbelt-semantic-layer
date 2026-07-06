-- ============================================================================
-- OrionBelt + Dremio - curated demo queries
-- ============================================================================
-- Run-order tells a story:
--   raw == governed  ->  filters just work  ->  the fan-trap OBSL prevents
--   ->  governed metrics  ->  period-over-period  ->  cross-fact derived metrics.
--
-- All queries verified live against demo/dremio (run-demo.sh), in Dremio's
-- SQL Runner (http://localhost:19047). The same queries also run against
-- OrionBelt directly (playground :17860, or any Postgres client on :15432
-- with database `orionbelt`) using `FROM model` instead of `orionbelt.commerce.model`.
-- ============================================================================


-- ============================================================================
-- A. IN DREMIO'S SQL RUNNER  (the federation story)
-- ============================================================================

-- A1. RAW lakehouse SQL over the Parquet in S3/MinIO.
--     "Dremio reads the Parquet directly."
SELECT co.countryname, SUM(s.salesamount) AS total_sales
FROM lake.commerce.sales s
JOIN lake.commerce.clients   c  ON s.salesclient = c.clientid
JOIN lake.commerce.countries co ON c.clientcountryid = co.countryid
GROUP BY co.countryname
ORDER BY total_sales DESC
LIMIT 5;
-- Singapore 9284889.34 | Mexico 5142120.39 | Sweden 3077505.53 | France 2942833.39 | Japan 2106985.67


-- A2. The SAME answer, governed - federated through OrionBelt.
--     No join, no aggregation spelled out: a business dimension + measure.
--     OrionBelt resolves the joins, applies the measure, compiles Dremio SQL,
--     and pushes it back into Dremio via Arrow Flight. Identical numbers.
SELECT "Country Name", "Total Sales"
FROM orionbelt.commerce.model
ORDER BY "Total Sales" DESC
LIMIT 5;
-- Singapore 9284889.34 | Mexico 5142120.39 | Sweden 3077505.53 | France 2942833.39 | Japan 2106985.67


-- A3. Filters push down through federation (v2.11.0).
--     Dremio wraps WHERE in a derived table; OrionBelt flattens it back.
--     A3a - dimension filter -> WHERE:
SELECT "Client Name", "Total Sales"
FROM orionbelt.commerce.model
WHERE "Country Name" = 'Singapore'
ORDER BY "Total Sales" DESC
LIMIT 5;
-- Val Ivanov 8622675.58 | Casey Bauer 143617.15 | Blake Klein 119829.94 | ...

--     A3b - measure filter -> HAVING:
SELECT "Country Name", "Total Sales"
FROM orionbelt.commerce.model
WHERE "Total Sales" > 1000000
ORDER BY "Total Sales" DESC
LIMIT 5;


-- A4. Cross-fact, no fan-trap (the differentiator).
--     Two measures from two different fact tables (sales + shipments).
--     OrionBelt detects independent facts and compiles a Composite Fact Layer
--     (UNION ALL with NULL padding) - correct per-grain totals.
SELECT "Year Month", "Total Sales", "Total Shipments"
FROM orionbelt.commerce.model
ORDER BY "Year Month"
LIMIT 12;
-- 2021-01  281222.38  266530.16 | 2021-02  439072.94  401102.95 | ...
--
-- Counter-example to show why this matters - hand-joining the two facts
-- silently corrupts BOTH numbers (drops/duplicates rows):
SELECT ca."ym", SUM(s.salesamount) AS total_sales, SUM(sh.shipmentamount) AS total_shipped
FROM lake.commerce.sales s
JOIN lake.commerce.shipments sh ON s.salesid = sh.shipmentsalesid
JOIN lake.commerce.calendar  ca ON s.salesdate = ca."date"
GROUP BY ca."ym"
ORDER BY ca."ym"
LIMIT 4;


-- A5. A governed metric, defined once in the model.
--     "Average Sale" = Total Sales / Sales Count - not in the query.
SELECT "Channel Name", "Total Sales", "Average Sale"
FROM orionbelt.commerce.model
ORDER BY "Total Sales" DESC;
-- Online 23009275.95 4542.80 | Retail 13269028.46 4524.05 | Wholesale ... 2980.73 | B2B ... 3381.46


-- A6. Period-over-period window metrics - month-over-month AND year-over-year
--     in one query. OrionBelt builds a date spine + a separate prior-period
--     self-join per offset; the consumer just names the metrics. Works through
--     federation as of v2.11.0.
SELECT "Sales Month", "Total Sales", "Sales MoM Change", "Sales YoY Growth"
FROM orionbelt.commerce.model
ORDER BY "Sales Month"
LIMIT 15;
-- 2021-01 .. (MoM null, YoY null) | 2021-12 1071384.97 403173.31 (YoY null)
-- | 2022-01 560521.77 -510863.20 0.9932 (first YoY: vs 2021-01)


-- A7. Cross-fact derived metrics (Returns / Sales, Sales - Cost).
--     Each combines measures from different fact tables; OrionBelt computes
--     the components inside a Composite Fact Layer and projects only what was
--     asked for. Works through federation as of v2.11.0.
SELECT "Product Category", "Total Sales", "Return Rate", "Gross Margin"
FROM orionbelt.commerce.model
ORDER BY "Total Sales" DESC
LIMIT 5;
-- Electronics 15307596.16 0.0463 -16050258.53 | Automotive 6588246.00 0.0229 -20403470.50 | ...


-- ============================================================================
-- Notes
-- ============================================================================
-- - The OrionBelt playground (http://localhost:17860) shows the loaded model
--   read-only and runs the same queries, plus the ER diagram and RDF graph.
-- - Any Postgres client can hit OrionBelt directly on localhost:15432
--   (database `orionbelt`), using `FROM model` instead of `orionbelt.commerce.model`.
-- - Period-over-period metrics may mix offsets (MoM + YoY) in one query; they
--   just need to share the time dimension and base grain (the date spine).

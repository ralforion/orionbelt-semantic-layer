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
-- with database = commerce) using `FROM model` instead of `obsl.commerce.model`.
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
FROM obsl.commerce.model
ORDER BY "Total Sales" DESC
LIMIT 5;
-- Singapore 9284889.34 | Mexico 5142120.39 | Sweden 3077505.53 | France 2942833.39 | Japan 2106985.67


-- A3. Filters push down through federation (v2.11.0).
--     Dremio wraps WHERE in a derived table; OrionBelt flattens it back.
--     A3a - dimension filter -> WHERE:
SELECT "Client Name", "Total Sales"
FROM obsl.commerce.model
WHERE "Country Name" = 'Singapore'
ORDER BY "Total Sales" DESC
LIMIT 5;
-- Val Ivanov 8622675.58 | Casey Bauer 143617.15 | Blake Klein 119829.94 | ...

--     A3b - measure filter -> HAVING:
SELECT "Country Name", "Total Sales"
FROM obsl.commerce.model
WHERE "Total Sales" > 1000000
ORDER BY "Total Sales" DESC
LIMIT 5;


-- A4. Cross-fact, no fan-trap (the differentiator).
--     Two measures from two different fact tables (sales + shipments).
--     OrionBelt detects independent facts and compiles a Composite Fact Layer
--     (UNION ALL with NULL padding) - correct per-grain totals.
SELECT "Year Month", "Total Sales", "Total Shipments"
FROM obsl.commerce.model
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
--     "Average Sale" = Total Sales / Sales Order Count - not in the query.
SELECT "Channel Name", "Total Sales", "Average Sale"
FROM obsl.commerce.model
ORDER BY "Total Sales" DESC;
-- Online 23009275.95 4542.80 | Retail 13269028.46 4524.05 | Wholesale ... 2980.73 | B2B ... 3381.46


-- A6. Period-over-period - month-over-month change (a window metric).
--     OrionBelt builds a date spine + self-join under the hood; the consumer
--     just asks for the metric. Works through federation as of v2.11.0.
SELECT "Sales Month", "Total Sales", "Sales MoM Change"
FROM obsl.commerce.model
ORDER BY "Sales Month"
LIMIT 12;
-- 2021-01  281222.38  (null) | 2021-02  439072.94  157850.56 | 2021-03  302261.62  -136811.32 | ...


-- A7. Cross-fact derived metrics (Returns / Sales, Sales - Cost).
--     Each combines measures from different fact tables; OrionBelt computes
--     the components inside a Composite Fact Layer and projects only what was
--     asked for. Works through federation as of v2.11.0.
SELECT "Product Category", "Total Sales", "Return Rate", "Gross Margin"
FROM obsl.commerce.model
ORDER BY "Total Sales" DESC
LIMIT 5;
-- Electronics 15307596.16 0.0463 -16050258.53 | Automotive 6588246.00 0.0229 -20403470.50 | ...


-- ============================================================================
-- Notes
-- ============================================================================
-- - The OrionBelt playground (http://localhost:17860) shows the loaded model
--   read-only and runs the same queries, plus the ER diagram and RDF graph.
-- - Any Postgres client can hit OrionBelt directly on localhost:15432
--   (database = commerce), using `FROM model` instead of `obsl.commerce.model`.
-- - Present one period-over-period metric at a time. Each works on its own
--   (Sales MoM Change, Sales YoY Growth, Sales Previous Year); combining metrics
--   of different period grains (e.g. MoM + YoY) in one query is not supported.

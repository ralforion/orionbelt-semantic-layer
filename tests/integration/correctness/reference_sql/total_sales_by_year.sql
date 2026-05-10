-- Bonus: Total Sales by Sales Year — time-grain hand reference.
-- Catches DATE_TRUNC bugs at the *value* level (e.g. wrong unit, wrong
-- start-of-year semantics, off-by-one TZ shift). The Phase 2 hierarchical
-- rollup test only proves year == sum(months); it does not catch a
-- planner that consistently uses 'quarter' where 'year' is intended.
SELECT DATE_TRUNC('year', s.salesdate) AS "Sales Year",
       CAST(SUM(s.salesamount) AS DECIMAL(18, 2)) AS "Total Sales"
FROM orionbelt_1.sales s
GROUP BY DATE_TRUNC('year', s.salesdate)

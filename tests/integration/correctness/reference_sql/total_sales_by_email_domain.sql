-- Corpus #16: Total Sales by the domain part of the client's email address.
-- The dimension is a computed column, not a stored one:
--   Client Email Domain: split_part({Client Email}, '@', 2)
-- Reference SQL written by hand against the orionbelt_1 schema, using
-- DuckDB's own SPLIT_PART — the canonical form the catalog is defined
-- against, so this file also states what the other dialects' rewrites
-- have to reproduce.
-- Compares against OBSL's compiled output for:
--   QueryObject(select=QuerySelect(dimensions=["Sales Client Email Domain"], measures=["Total Sales"]))
SELECT SPLIT_PART(c.clientemail, '@', 2) AS "Sales Client Email Domain",
       CAST(SUM(s.salesamount) AS DECIMAL(18, 2)) AS "Total Sales"
FROM orionbelt_1.sales s
LEFT JOIN orionbelt_1.clients c ON s.salesclient = c.clientid
GROUP BY SPLIT_PART(c.clientemail, '@', 2)

-- Corpus #6: Total Sales by Sales Client Name (role-playing dim, Sales→Clients path)
-- Forces the planner to take the Sales-routed Clients path. A bug where the
-- via: directive routes through the wrong fact would surface as different
-- per-client totals.
SELECT c.clientname AS "Sales Client Name",
       CAST(SUM(s.salesamount) AS DECIMAL(18, 2)) AS "Total Sales"
FROM orionbelt_1.sales s
LEFT JOIN orionbelt_1.clients c ON s.salesclient = c.clientid
GROUP BY c.clientname

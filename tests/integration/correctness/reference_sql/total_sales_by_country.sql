-- Corpus #2: Total Sales by Country (sales→clients→countries)
-- Reference SQL written by hand against orionbelt_1 schema.
-- Compares against OBSL's compiled output for:
--   QueryObject(select=QuerySelect(dimensions=["Sales Country Name"], measures=["Total Sales"]))
-- The CAST mirrors the measure's declared dataType (decimal(18, 2)).
SELECT co.countryname AS "Sales Country Name",
       CAST(SUM(s.salesamount) AS DECIMAL(18, 2)) AS "Total Sales"
FROM orionbelt_1.sales s
LEFT JOIN orionbelt_1.clients   c  ON s.salesclient    = c.clientid
LEFT JOIN orionbelt_1.countries co ON c.clientcountryid = co.countryid
GROUP BY co.countryname

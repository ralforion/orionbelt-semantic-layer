SELECT CASE WHEN CAST(CASE WHEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) = 0 AND ABS(CAST(SUM("Sales"."salesamount") AS DOUBLE)) >= CAST('1E-2' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) END AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
WHERE "Countries"."countryname" IN ('Croatia', 'Austria')

SELECT CASE WHEN CAST(CASE WHEN ABS(CAST(SUM("Sales"."salesamount") AS DECIMAL(38, 2))) >= 10000000000000000 THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) END AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
WHERE "Countries"."countryname" IN ('Croatia', 'Austria')

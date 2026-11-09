SELECT "Clients"."clientname" AS "Sales Client Name", CASE WHEN CAST(CASE WHEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) = 0 AND ABS(CAST(SUM("Sales"."salesamount") AS DOUBLE)) >= CAST('1E-2' AS DOUBLE) OR ABS(CAST(SUM("Sales"."salesamount") AS DECIMAL(38, 2))) >= 10000000000000000 THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) END AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
GROUP BY "Clients"."clientname"

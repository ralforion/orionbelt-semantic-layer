WITH "composite_01" AS (
SELECT "Countries"."countryname" AS "Sales Country Name", "Sales"."salesamount" AS "Total Sales", CAST(NULL AS DECIMAL(38, 20)) AS "Total Returns"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
UNION ALL
SELECT "Countries"."countryname" AS "Sales Country Name", CAST(NULL AS DECIMAL(38, 20)) AS "Total Sales", "Returns"."returnamount" AS "Total Returns"
FROM "orionbelt_1"."returns" AS "Returns"
LEFT JOIN "orionbelt_1"."sales" AS "Sales" ON "Returns"."returnsalesid" = "Sales"."salesid"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
)
SELECT "Sales Country Name" AS "Sales Country Name", CASE WHEN CAST(CASE WHEN ABS(CAST(SUM("composite_01"."Total Sales") AS DECIMAL(38, 2))) >= 10000000000000000 THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("composite_01"."Total Sales") AS DECIMAL(18, 2)) END AS "Total Sales", CASE WHEN CAST(CASE WHEN ABS(CAST(SUM("composite_01"."Total Returns") AS DECIMAL(38, 2))) >= 10000000000000000 THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("composite_01"."Total Returns") AS DECIMAL(18, 2)) END AS "Total Returns"
FROM "composite_01" AS "composite_01"
GROUP BY "Sales Country Name"

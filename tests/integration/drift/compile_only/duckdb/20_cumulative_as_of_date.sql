WITH "cumulative_base" AS (
SELECT "Regions"."regionname" AS "Sales Region Name", CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
LEFT JOIN "orionbelt_1"."regions" AS "Regions" ON "Countries"."region" = "Regions"."regionid"
GROUP BY ALL
),
"cumulative_as_of_periods" AS (
SELECT "Regions"."regionname" AS "Sales Region Name", CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE) AS "Sales Month", CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
LEFT JOIN "orionbelt_1"."regions" AS "Regions" ON "Countries"."region" = "Regions"."regionid"
GROUP BY ALL
),
"cumulative_as_of" AS (
SELECT "cumulative_as_of_periods"."Sales Region Name" AS "Sales Region Name", SUM(CASE WHEN DATE_DIFF('year', "cumulative_as_of_periods"."Sales Month", CAST('2021-03-18' AS DATE)) = 0 THEN "cumulative_as_of_periods"."Total Sales" END) AS "YTD Sales", SUM("cumulative_as_of_periods"."Total Sales") AS "Cumulative Sales"
FROM "cumulative_as_of_periods" AS "cumulative_as_of_periods"
WHERE DATE_DIFF('month', "cumulative_as_of_periods"."Sales Month", CAST('2021-03-18' AS DATE)) >= 0
GROUP BY ALL
),
"cumulative_as_of_periods_2" AS (
SELECT "Regions"."regionname" AS "Sales Region Name", CAST(DATE_TRUNC('day', "Sales"."salesdate") AS DATE) AS "Sales Date", CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
LEFT JOIN "orionbelt_1"."regions" AS "Regions" ON "Countries"."region" = "Regions"."regionid"
GROUP BY ALL
),
"cumulative_as_of_2" AS (
SELECT "cumulative_as_of_periods_2"."Sales Region Name" AS "Sales Region Name", SUM(CASE WHEN DATE_DIFF('month', "cumulative_as_of_periods_2"."Sales Date", CAST('2021-03-18' AS DATE)) = 0 THEN "cumulative_as_of_periods_2"."Total Sales" END) AS "MTD Sales", AVG(CASE WHEN DATE_DIFF('day', "cumulative_as_of_periods_2"."Sales Date", CAST('2021-03-18' AS DATE)) <= 29 THEN "cumulative_as_of_periods_2"."Total Sales" END) AS "Rolling 30 Day Sales", MAX(CASE WHEN DATE_DIFF('day', "cumulative_as_of_periods_2"."Sales Date", CAST('2021-03-18' AS DATE)) <= 29 THEN "cumulative_as_of_periods_2"."Total Sales" END) AS "Peak Daily Sales 30D"
FROM "cumulative_as_of_periods_2" AS "cumulative_as_of_periods_2"
WHERE DATE_DIFF('day', "cumulative_as_of_periods_2"."Sales Date", CAST('2021-03-18' AS DATE)) >= 0
GROUP BY ALL
),
"cumulative_joined" AS (
SELECT "cumulative_base"."Sales Region Name" AS "Sales Region Name", "cumulative_base"."Total Sales" AS "Total Sales", "cumulative_as_of"."YTD Sales" AS "YTD Sales", "cumulative_as_of"."Cumulative Sales" AS "Cumulative Sales", "cumulative_as_of_2"."MTD Sales" AS "MTD Sales", "cumulative_as_of_2"."Rolling 30 Day Sales" AS "Rolling 30 Day Sales", "cumulative_as_of_2"."Peak Daily Sales 30D" AS "Peak Daily Sales 30D"
FROM "cumulative_base" AS "cumulative_base"
LEFT JOIN "cumulative_as_of" AS "cumulative_as_of" ON "cumulative_base"."Sales Region Name" = "cumulative_as_of"."Sales Region Name" OR "cumulative_base"."Sales Region Name" IS NULL AND "cumulative_as_of"."Sales Region Name" IS NULL
LEFT JOIN "cumulative_as_of_2" AS "cumulative_as_of_2" ON "cumulative_base"."Sales Region Name" = "cumulative_as_of_2"."Sales Region Name" OR "cumulative_base"."Sales Region Name" IS NULL AND "cumulative_as_of_2"."Sales Region Name" IS NULL
)
SELECT "Sales Region Name" AS "Sales Region Name", "Total Sales" AS "Total Sales", CAST("YTD Sales" AS DECIMAL(18, 2)) AS "YTD Sales", CAST("Cumulative Sales" AS DECIMAL(18, 2)) AS "Cumulative Sales", CAST("MTD Sales" AS DECIMAL(18, 2)) AS "MTD Sales", CAST("Rolling 30 Day Sales" AS DECIMAL(18, 0)) AS "Rolling 30 Day Sales", CAST("Peak Daily Sales 30D" AS DECIMAL(18, 2)) AS "Peak Daily Sales 30D"
FROM "cumulative_joined" AS "cumulative_joined"

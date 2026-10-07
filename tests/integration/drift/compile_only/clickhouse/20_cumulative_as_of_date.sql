WITH "cumulative_base" AS (
SELECT "Regions"."regionname" AS "Sales Region Name", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
LEFT JOIN "orionbelt_1"."regions" AS "Regions" ON "Countries"."region" = "Regions"."regionid"
GROUP BY ALL
),
"cumulative_as_of_periods" AS (
SELECT "Regions"."regionname" AS "Sales Region Name", CAST(toStartOfMonth("Sales"."salesdate") AS Nullable(Date)) AS "Sales Month", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
LEFT JOIN "orionbelt_1"."regions" AS "Regions" ON "Countries"."region" = "Regions"."regionid"
GROUP BY ALL
),
"cumulative_as_of" AS (
SELECT "cumulative_as_of_periods"."Sales Region Name" AS "Sales Region Name", SUM(CASE WHEN date_diff('year', "cumulative_as_of_periods"."Sales Month", CAST(toStartOfMonth(CAST('2021-03-18' AS Nullable(Date))) AS Nullable(Date))) = 0 THEN "cumulative_as_of_periods"."Total Sales" END) AS "YTD Sales", SUM("cumulative_as_of_periods"."Total Sales") AS "Cumulative Sales"
FROM "cumulative_as_of_periods" AS "cumulative_as_of_periods"
WHERE date_diff('month', "cumulative_as_of_periods"."Sales Month", CAST(toStartOfMonth(CAST('2021-03-18' AS Nullable(Date))) AS Nullable(Date))) >= 0
GROUP BY ALL
),
"cumulative_as_of_periods_2" AS (
SELECT "Regions"."regionname" AS "Sales Region Name", CAST(toDate("Sales"."salesdate") AS Nullable(Date)) AS "Sales Date", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
LEFT JOIN "orionbelt_1"."regions" AS "Regions" ON "Countries"."region" = "Regions"."regionid"
GROUP BY ALL
),
"cumulative_as_of_2" AS (
SELECT "cumulative_as_of_periods_2"."Sales Region Name" AS "Sales Region Name", SUM(CASE WHEN date_diff('month', "cumulative_as_of_periods_2"."Sales Date", CAST('2021-03-18' AS Nullable(Date))) = 0 THEN "cumulative_as_of_periods_2"."Total Sales" END) AS "MTD Sales", AVG(CASE WHEN date_diff('day', "cumulative_as_of_periods_2"."Sales Date", CAST('2021-03-18' AS Nullable(Date))) <= 29 THEN "cumulative_as_of_periods_2"."Total Sales" END) AS "Rolling 30 Day Sales", MAX(CASE WHEN date_diff('day', "cumulative_as_of_periods_2"."Sales Date", CAST('2021-03-18' AS Nullable(Date))) <= 29 THEN "cumulative_as_of_periods_2"."Total Sales" END) AS "Peak Daily Sales 30D"
FROM "cumulative_as_of_periods_2" AS "cumulative_as_of_periods_2"
WHERE date_diff('day', "cumulative_as_of_periods_2"."Sales Date", CAST('2021-03-18' AS Nullable(Date))) >= 0
GROUP BY ALL
),
"cumulative_joined" AS (
SELECT "cumulative_base"."Sales Region Name" AS "Sales Region Name", "cumulative_base"."Total Sales" AS "Total Sales", "cumulative_as_of"."YTD Sales" AS "YTD Sales", "cumulative_as_of"."Cumulative Sales" AS "Cumulative Sales", "cumulative_as_of_2"."MTD Sales" AS "MTD Sales", "cumulative_as_of_2"."Rolling 30 Day Sales" AS "Rolling 30 Day Sales", "cumulative_as_of_2"."Peak Daily Sales 30D" AS "Peak Daily Sales 30D"
FROM "cumulative_base" AS "cumulative_base"
LEFT JOIN "cumulative_as_of" AS "cumulative_as_of" ON "cumulative_base"."Sales Region Name" = "cumulative_as_of"."Sales Region Name" OR "cumulative_base"."Sales Region Name" IS NULL AND "cumulative_as_of"."Sales Region Name" IS NULL
LEFT JOIN "cumulative_as_of_2" AS "cumulative_as_of_2" ON "cumulative_base"."Sales Region Name" = "cumulative_as_of_2"."Sales Region Name" OR "cumulative_base"."Sales Region Name" IS NULL AND "cumulative_as_of_2"."Sales Region Name" IS NULL
)
SELECT "Sales Region Name" AS "Sales Region Name", "Total Sales" AS "Total Sales", CAST(round(toDecimal256(toString("YTD Sales"), 3), 2) AS Nullable(Decimal(18, 2))) AS "YTD Sales", CAST(round(toDecimal256(toString("Cumulative Sales"), 3), 2) AS Nullable(Decimal(18, 2))) AS "Cumulative Sales", CAST(round(toDecimal256(toString("MTD Sales"), 3), 2) AS Nullable(Decimal(18, 2))) AS "MTD Sales", CAST(round(toDecimal256(toString("Rolling 30 Day Sales"), 1), 0) AS Nullable(Decimal(18, 0))) AS "Rolling 30 Day Sales", CAST(round(toDecimal256(toString("Peak Daily Sales 30D"), 3), 2) AS Nullable(Decimal(18, 2))) AS "Peak Daily Sales 30D"
FROM "cumulative_joined" AS "cumulative_joined"

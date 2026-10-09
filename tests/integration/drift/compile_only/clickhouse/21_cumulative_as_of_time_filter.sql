WITH "cumulative_base" AS (
SELECT "Regions"."regionname" AS "Sales Region Name", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
LEFT JOIN "orionbelt_1"."regions" AS "Regions" ON "Countries"."region" = "Regions"."regionid"
WHERE "Sales"."salesdate" >= '2022-03-01' AND "Sales"."salesdate" < '2022-07-01'
GROUP BY ALL
),
"cumulative_as_of_shown" AS (
SELECT "Regions"."regionname" AS "Sales Region Name", CAST(toStartOfMonth("Sales"."salesdate") AS Nullable(Date)) AS "Sales Month", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
LEFT JOIN "orionbelt_1"."countries" AS "Countries" ON "Clients"."clientcountryid" = "Countries"."countryid"
LEFT JOIN "orionbelt_1"."regions" AS "Regions" ON "Countries"."region" = "Regions"."regionid"
WHERE "Sales"."salesdate" >= '2022-03-01' AND "Sales"."salesdate" < '2022-07-01'
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
"cumulative_as_of_point" AS (
SELECT MAX("Sales Month") AS "as_of"
FROM "cumulative_as_of_shown" AS "cumulative_as_of_shown"
),
"cumulative_as_of" AS (
SELECT "cumulative_as_of_periods"."Sales Region Name" AS "Sales Region Name", SUM(CASE WHEN date_diff('year', "cumulative_as_of_periods"."Sales Month", "cumulative_as_of_point"."as_of") = 0 THEN "cumulative_as_of_periods"."Total Sales" END) AS "YTD Sales", SUM("cumulative_as_of_periods"."Total Sales") AS "Cumulative Sales"
FROM "cumulative_as_of_periods" AS "cumulative_as_of_periods"
CROSS JOIN "cumulative_as_of_point" AS "cumulative_as_of_point"
WHERE date_diff('month', "cumulative_as_of_periods"."Sales Month", "cumulative_as_of_point"."as_of") >= 0
GROUP BY ALL
),
"cumulative_joined" AS (
SELECT "cumulative_base"."Sales Region Name" AS "Sales Region Name", "cumulative_base"."Total Sales" AS "Total Sales", "cumulative_as_of"."YTD Sales" AS "YTD Sales", "cumulative_as_of"."Cumulative Sales" AS "Cumulative Sales"
FROM "cumulative_base" AS "cumulative_base"
LEFT JOIN "cumulative_as_of" AS "cumulative_as_of" ON isNotDistinctFrom("cumulative_base"."Sales Region Name", "cumulative_as_of"."Sales Region Name")
)
SELECT "Sales Region Name" AS "Sales Region Name", "Total Sales" AS "Total Sales", CAST(round(toDecimal256(toString("YTD Sales"), 3), 2) AS Nullable(Decimal(18, 2))) AS "YTD Sales", CAST(round(toDecimal256(toString("Cumulative Sales"), 3), 2) AS Nullable(Decimal(18, 2))) AS "Cumulative Sales"
FROM "cumulative_joined" AS "cumulative_joined"

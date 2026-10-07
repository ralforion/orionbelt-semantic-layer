WITH "cumulative_base" AS (
SELECT CAST(toDate("Sales"."salesdate") AS Nullable(Date)) AS "Sales Date", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
WHERE "Sales"."salesdate" >= '2021-02-01' AND "Sales"."salesdate" < '2021-03-01'
GROUP BY ALL
),
"cumulative_lookback" AS (
SELECT CAST(toDate("Sales"."salesdate") AS Nullable(Date)) AS "Sales Date", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
),
"cumulative_rolling" AS (
SELECT "cumulative_current"."Sales Date" AS "Sales Date", AVG("cumulative_prior"."Total Sales") AS "Rolling 30 Day Sales", MAX("cumulative_prior"."Total Sales") AS "Peak Daily Sales 30D"
FROM "cumulative_lookback" AS "cumulative_current"
INNER JOIN "cumulative_lookback" AS "cumulative_prior" ON date_diff('day', "cumulative_prior"."Sales Date", "cumulative_current"."Sales Date") >= 0 AND date_diff('day', "cumulative_prior"."Sales Date", "cumulative_current"."Sales Date") <= 29 OR "cumulative_current"."Sales Date" IS NULL AND "cumulative_prior"."Sales Date" IS NULL
GROUP BY ALL
),
"cumulative_joined" AS (
SELECT "cumulative_base"."Sales Date" AS "Sales Date", "cumulative_base"."Total Sales" AS "Total Sales", "cumulative_rolling"."Rolling 30 Day Sales" AS "Rolling 30 Day Sales", "cumulative_rolling"."Peak Daily Sales 30D" AS "Peak Daily Sales 30D"
FROM "cumulative_base" AS "cumulative_base"
LEFT JOIN "cumulative_rolling" AS "cumulative_rolling" ON "cumulative_base"."Sales Date" = "cumulative_rolling"."Sales Date" OR "cumulative_base"."Sales Date" IS NULL AND "cumulative_rolling"."Sales Date" IS NULL
)
SELECT "Sales Date" AS "Sales Date", "Total Sales" AS "Total Sales", CAST(round(toDecimal256(toString("Rolling 30 Day Sales"), 1), 0) AS Nullable(Decimal(18, 0))) AS "Rolling 30 Day Sales", CAST(round(toDecimal256(toString("Peak Daily Sales 30D"), 3), 2) AS Nullable(Decimal(18, 2))) AS "Peak Daily Sales 30D"
FROM "cumulative_joined" AS "cumulative_joined"

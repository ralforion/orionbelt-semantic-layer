WITH "cumulative_base" AS (
SELECT CAST(toDate("Sales"."salesdate") AS Nullable(Date)) AS "Sales Date", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
),
"cumulative_rolling" AS (
SELECT "cumulative_current"."Sales Date" AS "Sales Date", AVG("cumulative_prior"."Total Sales") AS "Rolling 30 Day Sales"
FROM "cumulative_base" AS "cumulative_current"
INNER JOIN "cumulative_base" AS "cumulative_prior" ON date_diff('day', "cumulative_prior"."Sales Date", "cumulative_current"."Sales Date") >= 0 AND date_diff('day', "cumulative_prior"."Sales Date", "cumulative_current"."Sales Date") <= 29 OR "cumulative_current"."Sales Date" IS NULL AND "cumulative_prior"."Sales Date" IS NULL
GROUP BY ALL
),
"cumulative_joined" AS (
SELECT "cumulative_base"."Sales Date" AS "Sales Date", "cumulative_rolling"."Rolling 30 Day Sales" AS "Rolling 30 Day Sales"
FROM "cumulative_base" AS "cumulative_base"
LEFT JOIN "cumulative_rolling" AS "cumulative_rolling" ON isNotDistinctFrom("cumulative_base"."Sales Date", "cumulative_rolling"."Sales Date")
)
SELECT "Sales Date" AS "Sales Date", CAST(round(toDecimal256(toString("Rolling 30 Day Sales"), 1), 0) AS Nullable(Decimal(18, 0))) AS "Rolling 30 Day Sales"
FROM "cumulative_joined" AS "cumulative_joined"

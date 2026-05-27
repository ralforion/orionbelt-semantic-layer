WITH "composite_01" AS (
SELECT "Clients"."clientname" AS "Sales Client Name", CAST(NULL AS Nullable(String)) AS "Complaint Client Name", CAST(round("Sales"."salesamount", 2) AS Nullable(Decimal(18, 2))) AS "Total Sales", CAST(NULL AS Nullable(String)) AS "Complaint Count"
FROM "orionbelt_1"."sales" AS "Sales"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Sales"."salesclient" = "Clients"."clientid"
UNION ALL
SELECT CAST(NULL AS Nullable(String)) AS "Sales Client Name", "Clients"."clientname" AS "Complaint Client Name", CAST(round(NULL, 2) AS Nullable(Decimal(18, 2))) AS "Total Sales", CAST("Client Complaints"."complid" AS Nullable(String)) AS "Complaint Count"
FROM "orionbelt_1"."clientcomplaints" AS "Client Complaints"
LEFT JOIN "orionbelt_1"."clients" AS "Clients" ON "Client Complaints"."complclientid" = "Clients"."clientid"
)
SELECT COALESCE("Sales Client Name", "Complaint Client Name") AS "Client", CAST(round(SUM("composite_01"."Total Sales"), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales", CAST(COUNT(DISTINCT "composite_01"."Complaint Count") AS Nullable(Int64)) AS "Complaint Count"
FROM "composite_01" AS "composite_01"
GROUP BY ALL
HAVING CAST(COUNT(DISTINCT "composite_01"."Complaint Count") AS Nullable(Int64)) > 0

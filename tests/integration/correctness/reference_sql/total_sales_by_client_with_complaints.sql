-- Corpus #7: Total Sales for clients with ≥1 complaint, expressed via the
-- ``CoalesceDimension`` form — both role-playing dims (`Sales Client
-- Name` via Sales, `Complaint Client Name` via Client Complaints) are
-- projected through their respective CFL legs and merged at the outer
-- SELECT/GROUP BY:
--
--    SELECT COALESCE("Sales Client Name", "Complaint Client Name") AS "Client",
--           ...
--    GROUP BY COALESCE("Sales Client Name", "Complaint Client Name")
--
-- HAVING Client Complaints Count > 0 enforces the membership filter.
--
-- Hand reference computes the two per-client aggregates independently
-- and INNER-JOINs on client_id to enforce the filter, avoiding the fan
-- multiplication a multi-LEFT-JOIN would introduce.
--
-- KNOWN ASSUMPTION (parked, not exercised by this test):
-- The current OBSL emit groups on COALESCE of the *display name*
-- (`clients.clientname`) rather than on `clients.clientid`. Two distinct
-- clients sharing a name would silently merge. Tracked as a separate
-- design concern; this hand reference mirrors the same name-grouping so
-- the cross-check stays valid against today's emit.
WITH sales_per_client AS (
    SELECT c.clientid,
           c.clientname,
           CAST(SUM(s.salesamount) AS DECIMAL(18, 2)) AS total_sales
    FROM orionbelt_1.clients c
    LEFT JOIN orionbelt_1.sales s ON s.salesclient = c.clientid
    GROUP BY c.clientid, c.clientname
),
complaints_per_client AS (
    SELECT cc.complclientid AS clientid,
           COUNT(DISTINCT cc.complid) AS complaint_count
    FROM orionbelt_1.clientcomplaints cc
    GROUP BY cc.complclientid
)
SELECT s.clientname             AS "Client",
       s.total_sales            AS "Total Sales",
       CAST(c.complaint_count AS BIGINT) AS "Client Complaints Count"
FROM sales_per_client s
INNER JOIN complaints_per_client c ON s.clientid = c.clientid
WHERE c.complaint_count > 0

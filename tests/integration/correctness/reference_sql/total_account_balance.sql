-- Corpus #15: Total Account Balance — single-fact ungrouped sum.
-- The simplest possible correctness anchor: confirms basic SUM aggregation
-- and value precision (decimal(18, 2)) against a fact table that does not
-- participate in any CFL or role-playing scenario.
SELECT CAST(SUM(balanceamt) AS DECIMAL(18, 2)) AS "Total Account Balance"
FROM orionbelt_1.acctbal

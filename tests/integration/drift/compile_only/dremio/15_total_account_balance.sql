SELECT CASE WHEN CAST(CASE WHEN CAST(SUM("Account Balances"."balanceamt") AS DECIMAL(18, 2)) = 0 AND ABS(CAST(SUM("Account Balances"."balanceamt") AS DOUBLE)) >= CAST('1E-2' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Account Balances"."balanceamt") AS DECIMAL(18, 2)) END AS "Total Account Balance"
FROM "orionbelt_1"."acctbal" AS "Account Balances"

SELECT CASE WHEN CAST(CASE WHEN ABS(CAST(SUM("Account Balances"."balanceamt") AS DECIMAL(38, 2))) >= 10000000000000000 THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Account Balances"."balanceamt") AS DECIMAL(18, 2)) END AS "Total Account Balance"
FROM "orionbelt_1"."acctbal" AS "Account Balances"

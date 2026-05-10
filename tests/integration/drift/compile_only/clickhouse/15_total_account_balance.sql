SELECT CAST(round(SUM("Account Balances"."balanceamt"), 2) AS Nullable(Decimal(18, 2))) AS "Total Account Balance"
FROM "orionbelt_1"."acctbal" AS "Account Balances"

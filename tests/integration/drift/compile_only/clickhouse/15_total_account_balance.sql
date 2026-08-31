SELECT CAST(round(toDecimal256(toString(SUM("Account Balances"."balanceamt")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Account Balance"
FROM "orionbelt_1"."acctbal" AS "Account Balances"

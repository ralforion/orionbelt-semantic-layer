SELECT ROUND(CAST(SUM(`Account Balances`.`balanceamt`) AS NUMERIC), 2) AS `Total Account Balance`
FROM ``.`orionbelt_1`.`acctbal` AS `Account Balances`

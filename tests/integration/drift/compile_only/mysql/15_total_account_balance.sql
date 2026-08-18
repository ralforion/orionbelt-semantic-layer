SELECT CAST(SUM(`Account Balances`.`balanceamt`) AS DECIMAL(38, 2)) AS `Total Account Balance`
FROM `orionbelt_1`.`acctbal` AS `Account Balances`

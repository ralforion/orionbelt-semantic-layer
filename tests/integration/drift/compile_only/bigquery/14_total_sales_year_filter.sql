SELECT ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
WHERE `Sales`.`salesdate` = '2025-01-01'

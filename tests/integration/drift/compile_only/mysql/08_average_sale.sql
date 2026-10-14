SELECT CAST(SUM(`Sales`.`salesamount`) / NULLIF(NULLIF(COUNT(1), 0), 0) AS DECIMAL(38, 2)) AS `Average Sale`
FROM `orionbelt_1`.`sales` AS `Sales`

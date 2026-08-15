SELECT CAST(SUM(`Sales`.`salesamount`) / NULLIF(COUNT(1), 0) AS DECIMAL(18, 2)) AS `Average Sale`
FROM `orionbelt_1`.`sales` AS `Sales`

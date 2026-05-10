SELECT CAST((SUM(`Sales`.`salesamount`) / COUNT(DISTINCT `Sales`.`salesid`)) AS DECIMAL(18, 2)) AS `Average Sale`
FROM ``.`orionbelt_1`.`sales` AS `Sales`

SELECT ROUND(CAST((SUM(`Sales`.`salesamount`) / COUNT(DISTINCT `Sales`.`salesid`)) AS NUMERIC), 2) AS `Average Sale`
FROM ``.`orionbelt_1`.`sales` AS `Sales`

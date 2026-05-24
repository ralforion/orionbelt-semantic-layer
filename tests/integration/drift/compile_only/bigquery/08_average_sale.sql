SELECT ROUND(CAST((SUM(`Sales`.`salesamount`) / NULLIF(COUNT(DISTINCT `Sales`.`salesid`), 0)) AS NUMERIC), 2) AS `Average Sale`
FROM ``.`orionbelt_1`.`sales` AS `Sales`

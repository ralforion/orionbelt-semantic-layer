SELECT DATE_TRUNC(`Sales`.`salesdate`, YEAR) AS `Sales Year`, DATE_TRUNC(`Sales`.`salesdate`, MONTH) AS `Sales Month`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
GROUP BY ALL

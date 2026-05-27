SELECT DATE_TRUNC(`Sales`.`salesdate`, 'year') AS `Sales Year`, DATE_TRUNC(`Sales`.`salesdate`, 'month') AS `Sales Month`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
GROUP BY ALL

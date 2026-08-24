SELECT CAST(DATE_TRUNC(`Sales`.`salesdate`, YEAR) AS DATE) AS `Sales Year`, CAST(DATE_TRUNC(`Sales`.`salesdate`, MONTH) AS DATE) AS `Sales Month`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY ALL

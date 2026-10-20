SELECT ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
WHERE CAST(DATE_TRUNC(`Sales`.`salesdate`, YEAR) AS DATE) = '2025-01-01'

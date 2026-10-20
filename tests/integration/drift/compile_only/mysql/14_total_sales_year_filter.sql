SELECT CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(38, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
WHERE CAST(CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-01-01') AS DATE) AS DATE) = '2025-01-01'

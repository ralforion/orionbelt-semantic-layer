SELECT CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-01-01') AS DATE) AS `Sales Year`, CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE) AS `Sales Month`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(38, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-01-01') AS DATE), CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE)

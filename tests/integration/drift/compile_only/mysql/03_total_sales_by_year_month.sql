SELECT DATE_FORMAT(`Sales`.`salesdate`, '%Y-01-01') AS `Sales Year`, DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS `Sales Month`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(18, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY DATE_FORMAT(`Sales`.`salesdate`, '%Y-01-01'), DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01')

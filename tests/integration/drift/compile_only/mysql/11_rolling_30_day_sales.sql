WITH `cumulative_base` AS (
SELECT CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-%d') AS DATE) AS `Sales Date`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(38, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-%d') AS DATE)
),
`cumulative_rolling` AS (
SELECT `cumulative_current`.`Sales Date` AS `Sales Date`, AVG(`cumulative_prior`.`Total Sales`) AS `Rolling 30 Day Sales`
FROM `cumulative_base` AS `cumulative_current`
INNER JOIN `cumulative_base` AS `cumulative_prior` ON TIMESTAMPDIFF(DAY, CAST(DATE_FORMAT(`cumulative_prior`.`Sales Date`, '%Y-%m-%d') AS DATE), CAST(DATE_FORMAT(`cumulative_current`.`Sales Date`, '%Y-%m-%d') AS DATE)) >= 0 AND TIMESTAMPDIFF(DAY, CAST(DATE_FORMAT(`cumulative_prior`.`Sales Date`, '%Y-%m-%d') AS DATE), CAST(DATE_FORMAT(`cumulative_current`.`Sales Date`, '%Y-%m-%d') AS DATE)) <= 29
GROUP BY `cumulative_current`.`Sales Date`
),
`cumulative_joined` AS (
SELECT `cumulative_base`.`Sales Date` AS `Sales Date`, `cumulative_rolling`.`Rolling 30 Day Sales` AS `Rolling 30 Day Sales`
FROM `cumulative_base` AS `cumulative_base`
LEFT JOIN `cumulative_rolling` AS `cumulative_rolling` ON `cumulative_base`.`Sales Date` = `cumulative_rolling`.`Sales Date` OR `cumulative_base`.`Sales Date` IS NULL AND `cumulative_rolling`.`Sales Date` IS NULL
)
SELECT `Sales Date` AS `Sales Date`, CAST(`Rolling 30 Day Sales` AS DECIMAL(38, 0)) AS `Rolling 30 Day Sales`
FROM `cumulative_joined` AS `cumulative_joined`

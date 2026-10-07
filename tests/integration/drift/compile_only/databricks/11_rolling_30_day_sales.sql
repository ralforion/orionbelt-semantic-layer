WITH `cumulative_base` AS (
SELECT CAST(DATE_TRUNC('day', `Sales`.`salesdate`) AS DATE) AS `Sales Date`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(18, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY ALL
),
`cumulative_rolling` AS (
SELECT `cumulative_current`.`Sales Date` AS `Sales Date`, AVG(`cumulative_prior`.`Total Sales`) AS `Rolling 30 Day Sales`
FROM `cumulative_base` AS `cumulative_current`
INNER JOIN `cumulative_base` AS `cumulative_prior` ON date_diff(DAY, `cumulative_prior`.`Sales Date`, `cumulative_current`.`Sales Date`) >= 0 AND date_diff(DAY, `cumulative_prior`.`Sales Date`, `cumulative_current`.`Sales Date`) <= 29 OR `cumulative_current`.`Sales Date` IS NULL AND `cumulative_prior`.`Sales Date` IS NULL
GROUP BY ALL
),
`cumulative_joined` AS (
SELECT `cumulative_base`.`Sales Date` AS `Sales Date`, `cumulative_rolling`.`Rolling 30 Day Sales` AS `Rolling 30 Day Sales`
FROM `cumulative_base` AS `cumulative_base`
LEFT JOIN `cumulative_rolling` AS `cumulative_rolling` ON `cumulative_base`.`Sales Date` = `cumulative_rolling`.`Sales Date` OR `cumulative_base`.`Sales Date` IS NULL AND `cumulative_rolling`.`Sales Date` IS NULL
)
SELECT `Sales Date` AS `Sales Date`, CAST(`Rolling 30 Day Sales` AS DECIMAL(18, 0)) AS `Rolling 30 Day Sales`
FROM `cumulative_joined` AS `cumulative_joined`

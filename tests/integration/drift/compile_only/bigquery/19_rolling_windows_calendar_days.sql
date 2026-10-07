WITH `cumulative_base` AS (
SELECT CAST(DATE_TRUNC(`Sales`.`salesdate`, DAY) AS DATE) AS `Sales Date`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
WHERE `Sales`.`salesdate` >= '2021-02-01' AND `Sales`.`salesdate` < '2021-03-01'
GROUP BY ALL
),
`cumulative_lookback` AS (
SELECT CAST(DATE_TRUNC(`Sales`.`salesdate`, DAY) AS DATE) AS `Sales Date`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY ALL
),
`cumulative_rolling` AS (
SELECT `cumulative_current`.`Sales Date` AS `Sales Date`, AVG(`cumulative_prior`.`Total Sales`) AS `Rolling 30 Day Sales`, MAX(`cumulative_prior`.`Total Sales`) AS `Peak Daily Sales 30D`
FROM `cumulative_lookback` AS `cumulative_current`
INNER JOIN `cumulative_lookback` AS `cumulative_prior` ON DATE_DIFF(CAST(`cumulative_current`.`Sales Date` AS DATETIME), CAST(`cumulative_prior`.`Sales Date` AS DATETIME), DAY) >= 0 AND DATE_DIFF(CAST(`cumulative_current`.`Sales Date` AS DATETIME), CAST(`cumulative_prior`.`Sales Date` AS DATETIME), DAY) <= 29 OR `cumulative_current`.`Sales Date` IS NULL AND `cumulative_prior`.`Sales Date` IS NULL
GROUP BY ALL
),
`cumulative_joined` AS (
SELECT `cumulative_base`.`Sales Date` AS `Sales Date`, `cumulative_base`.`Total Sales` AS `Total Sales`, `cumulative_rolling`.`Rolling 30 Day Sales` AS `Rolling 30 Day Sales`, `cumulative_rolling`.`Peak Daily Sales 30D` AS `Peak Daily Sales 30D`
FROM `cumulative_base` AS `cumulative_base`
LEFT JOIN `cumulative_rolling` AS `cumulative_rolling` ON `cumulative_base`.`Sales Date` = `cumulative_rolling`.`Sales Date` OR `cumulative_base`.`Sales Date` IS NULL AND `cumulative_rolling`.`Sales Date` IS NULL
)
SELECT `Sales Date` AS `Sales Date`, `Total Sales` AS `Total Sales`, ROUND(CAST(`Rolling 30 Day Sales` AS NUMERIC), 0) AS `Rolling 30 Day Sales`, ROUND(CAST(`Peak Daily Sales 30D` AS NUMERIC), 2) AS `Peak Daily Sales 30D`
FROM `cumulative_joined` AS `cumulative_joined`

WITH `cumulative_base` AS (
SELECT CAST(DATE_TRUNC(`Sales`.`salesdate`, MONTH) AS DATE) AS `Sales Month`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
WHERE `Sales`.`salesdate` >= '2021-03-01' AND `Sales`.`salesdate` < '2021-05-01'
GROUP BY ALL
),
`cumulative_lookback` AS (
SELECT CAST(DATE_TRUNC(`Sales`.`salesdate`, MONTH) AS DATE) AS `Sales Month`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY ALL
),
`cumulative_window` AS (
SELECT `Sales Month` AS `Sales Month`, SUM(`Total Sales`) OVER (PARTITION BY DATE_TRUNC(`Sales Month`, YEAR) ORDER BY `Sales Month` ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS `YTD Sales`
FROM `cumulative_lookback` AS `cumulative_lookback`
),
`cumulative_joined` AS (
SELECT `cumulative_base`.`Sales Month` AS `Sales Month`, `cumulative_base`.`Total Sales` AS `Total Sales`, `cumulative_window`.`YTD Sales` AS `YTD Sales`
FROM `cumulative_base` AS `cumulative_base`
LEFT JOIN `cumulative_window` AS `cumulative_window` ON `cumulative_base`.`Sales Month` = `cumulative_window`.`Sales Month` OR `cumulative_base`.`Sales Month` IS NULL AND `cumulative_window`.`Sales Month` IS NULL
)
SELECT `Sales Month` AS `Sales Month`, `Total Sales` AS `Total Sales`, ROUND(CAST(`YTD Sales` AS NUMERIC), 2) AS `YTD Sales`
FROM `cumulative_joined` AS `cumulative_joined`

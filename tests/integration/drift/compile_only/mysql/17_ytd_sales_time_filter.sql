WITH `cumulative_base` AS (
SELECT CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE) AS `Sales Month`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(38, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
WHERE `Sales`.`salesdate` >= '2021-03-01' AND `Sales`.`salesdate` < '2021-05-01'
GROUP BY CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE)
),
`cumulative_lookback` AS (
SELECT CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE) AS `Sales Month`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(38, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE)
),
`cumulative_window` AS (
SELECT `Sales Month` AS `Sales Month`, SUM(`Total Sales`) OVER (PARTITION BY CAST(DATE_FORMAT(`Sales Month`, '%Y-01-01') AS DATE) ORDER BY `Sales Month` ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS `YTD Sales`
FROM `cumulative_lookback` AS `cumulative_lookback`
),
`cumulative_joined` AS (
SELECT `cumulative_base`.`Sales Month` AS `Sales Month`, `cumulative_base`.`Total Sales` AS `Total Sales`, `cumulative_window`.`YTD Sales` AS `YTD Sales`
FROM `cumulative_base` AS `cumulative_base`
LEFT JOIN `cumulative_window` AS `cumulative_window` ON `cumulative_base`.`Sales Month` = `cumulative_window`.`Sales Month` OR `cumulative_base`.`Sales Month` IS NULL AND `cumulative_window`.`Sales Month` IS NULL
)
SELECT `Sales Month` AS `Sales Month`, `Total Sales` AS `Total Sales`, CAST(`YTD Sales` AS DECIMAL(38, 2)) AS `YTD Sales`
FROM `cumulative_joined` AS `cumulative_joined`

WITH `cumulative_base` AS (
SELECT DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-%d') AS `Sales Date`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(18, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-%d')
)
SELECT `Sales Date` AS `Sales Date`, CAST(AVG(`Total Sales`) OVER (ORDER BY `Sales Date` ASC ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS DECIMAL(18, 2)) AS `Rolling 30 Day Sales`
FROM cumulative_base AS `cumulative_base`

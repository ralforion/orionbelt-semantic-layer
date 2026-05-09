WITH `cumulative_base` AS (
SELECT DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS `Sales Month`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(18, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
GROUP BY DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01')
)
SELECT `Sales Month` AS `Sales Month`, CAST(SUM(`Total Sales`) OVER (ORDER BY `Sales Month` ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS DECIMAL(18, 2)) AS `Cumulative Sales`
FROM cumulative_base AS `cumulative_base`

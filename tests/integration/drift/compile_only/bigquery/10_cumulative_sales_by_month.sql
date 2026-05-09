WITH `cumulative_base` AS (
SELECT DATE_TRUNC(`Sales`.`salesdate`, 'month') AS `Sales Month`, CAST(SUM(`Sales`.`salesamount`) AS NUMERIC(18, 2)) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
GROUP BY DATE_TRUNC(`Sales`.`salesdate`, 'month')
)
SELECT `Sales Month` AS `Sales Month`, CAST(SUM(`Total Sales`) OVER (ORDER BY `Sales Month` ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS NUMERIC(18, 2)) AS `Cumulative Sales`
FROM cumulative_base AS `cumulative_base`

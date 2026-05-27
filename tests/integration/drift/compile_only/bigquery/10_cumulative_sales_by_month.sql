WITH `cumulative_base` AS (
SELECT DATE_TRUNC(`Sales`.`salesdate`, 'month') AS `Sales Month`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
GROUP BY ALL
)
SELECT `Sales Month` AS `Sales Month`, ROUND(CAST(SUM(`Total Sales`) OVER (ORDER BY `Sales Month` ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS NUMERIC), 2) AS `Cumulative Sales`
FROM `cumulative_base` AS `cumulative_base`

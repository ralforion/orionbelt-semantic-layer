WITH `cumulative_base` AS (
SELECT DATE_TRUNC(`Sales`.`salesdate`, DAY) AS `Sales Date`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
GROUP BY ALL
)
SELECT `Sales Date` AS `Sales Date`, ROUND(CAST(AVG(`Total Sales`) OVER (ORDER BY `Sales Date` ASC ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS NUMERIC), 0) AS `Rolling 30 Day Sales`
FROM `cumulative_base` AS `cumulative_base`

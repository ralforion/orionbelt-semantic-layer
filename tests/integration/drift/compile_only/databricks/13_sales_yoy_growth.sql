WITH `date_range` AS (
SELECT date_trunc('month', MIN(`Sales`.`salesdate`)) AS min_date,
       date_trunc('month', MAX(`Sales`.`salesdate`)) AS max_date
  FROM ``.`orionbelt_1`.`sales` AS `Sales`
),
`date_spine` AS (
SELECT d AS spine_date,
       CASE WHEN add_months(d, -12) >= (SELECT min_date FROM `date_range`)
            THEN add_months(d, -12) END AS spine_date_prev
FROM (SELECT EXPLODE(SEQUENCE((SELECT min_date FROM `date_range`), (SELECT max_date FROM `date_range`), INTERVAL 1 MONTH)) AS d)
),
`pop_base` AS (
SELECT `date_spine`.spine_date AS `Sales Month`,
       SUM(`Sales`.`salesamount`) AS `Total Sales`
  FROM `date_spine`
  LEFT JOIN ``.`orionbelt_1`.`sales` AS `Sales`
    ON date_trunc('month', `Sales`.`salesdate`) = `date_spine`.spine_date
  GROUP BY 1
),
`pop_compare` AS (
SELECT `pop_base`.`Sales Month` AS `Sales Month`,
       `pop_base`.`Total Sales` / NULLIF(pop_prev.`Total Sales`, 0) - 1 AS `Sales YoY Growth`
  FROM `pop_base`
  LEFT JOIN `date_spine` ON `pop_base`.`Sales Month` = `date_spine`.spine_date
  LEFT JOIN `pop_base` AS pop_prev
    ON `date_spine`.spine_date_prev = pop_prev.`Sales Month`
)
SELECT `Sales Month` AS `Sales Month`, `Sales YoY Growth` AS `Sales YoY Growth`
FROM `pop_compare` AS `pop_compare`

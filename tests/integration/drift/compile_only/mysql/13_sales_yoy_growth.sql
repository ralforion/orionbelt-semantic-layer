WITH `date_range` AS (
SELECT DATE_FORMAT(MIN(`Sales`.`salesdate`), '%Y-%m-01') AS min_date,
       DATE_FORMAT(MAX(`Sales`.`salesdate`), '%Y-%m-01') AS max_date
  FROM `orionbelt_1`.`sales` AS `Sales`
),
`date_spine` AS (
SELECT spine_date,
       CASE WHEN DATE_SUB(spine_date, INTERVAL 1 YEAR) >= (SELECT min_date FROM date_range)
            THEN DATE_SUB(spine_date, INTERVAL 1 YEAR) END AS spine_date_prev
FROM (
  WITH RECURSIVE dates AS (
    SELECT (SELECT min_date FROM date_range) AS spine_date
    UNION ALL
    SELECT DATE_ADD(spine_date, INTERVAL 1 MONTH)
    FROM dates WHERE spine_date < (SELECT max_date FROM date_range)
  )
  SELECT spine_date FROM dates
) AS spine
),
`pop_base` AS (
SELECT date_spine.spine_date AS `Sales Month`,
       SUM(`Sales`.`salesamount`) AS `Total Sales`
  FROM date_spine
  LEFT JOIN `orionbelt_1`.`sales` AS `Sales`
    ON DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') = date_spine.spine_date
  GROUP BY 1
),
`pop_compare` AS (
SELECT pop_base.`Sales Month` AS `Sales Month`,
       pop_base.`Total Sales` / NULLIF(prev.`Total Sales`, 0) - 1 AS `Sales YoY Growth`
  FROM pop_base
  LEFT JOIN date_spine ON pop_base.`Sales Month` = date_spine.spine_date
  LEFT JOIN pop_base AS prev
    ON date_spine.spine_date_prev = prev.`Sales Month`
)
SELECT `Sales Month` AS `Sales Month`, `Sales YoY Growth` AS `Sales YoY Growth`
FROM pop_compare AS `pop_compare`

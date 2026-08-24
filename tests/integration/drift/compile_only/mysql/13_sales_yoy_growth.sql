WITH `date_range` AS (
SELECT MIN(`__ob_pop_src`.`__ob_bucket`) AS min_date,
       MAX(`__ob_pop_src`.`__ob_bucket`) AS max_date
  FROM (
    SELECT DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS `__ob_bucket`
      FROM `orionbelt_1`.`sales` AS `Sales`
  ) AS `__ob_pop_src`
),
`date_spine` AS (
SELECT spine_date,
       CASE WHEN DATE_SUB(spine_date, INTERVAL 1 YEAR) >= (SELECT min_date FROM `date_range`)
            THEN DATE_SUB(spine_date, INTERVAL 1 YEAR) END AS spine_date_prev
FROM (
  WITH RECURSIVE dates AS (
    SELECT (SELECT min_date FROM `date_range`) AS spine_date
    UNION ALL
    SELECT DATE_ADD(spine_date, INTERVAL 1 MONTH)
    FROM dates WHERE spine_date < (SELECT max_date FROM `date_range`)
  )
  SELECT spine_date FROM dates
) AS spine
),
`pop_base` AS (
SELECT `date_spine`.spine_date AS `Sales Month`,
       CAST(SUM(`__ob_pop_src`.`Sales__salesamount`) AS DECIMAL(38, 2)) AS `Total Sales`
  FROM `date_spine`
  LEFT JOIN (
    SELECT DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS `__ob_bucket`,
           `Sales`.`salesamount` AS `Sales__salesamount`
      FROM `orionbelt_1`.`sales` AS `Sales`
  ) AS `__ob_pop_src`
    ON `__ob_pop_src`.`__ob_bucket` = `date_spine`.spine_date
  GROUP BY 1
),
`pop_compare` AS (
SELECT `pop_base`.`Sales Month` AS `Sales Month`,
       CAST(`pop_base`.`Total Sales` AS DECIMAL(38, 14)) / CAST(NULLIF(pop_prev.`Total Sales`, 0) AS DECIMAL(38, 14)) - 1 AS `Sales YoY Growth`
  FROM `pop_base`
  LEFT JOIN `date_spine` ON `pop_base`.`Sales Month` = `date_spine`.spine_date
  LEFT JOIN `pop_base` AS pop_prev
    ON `date_spine`.spine_date_prev = pop_prev.`Sales Month`
)
SELECT `Sales Month` AS `Sales Month`, CAST(`Sales YoY Growth` AS DECIMAL(38, 4)) AS `Sales YoY Growth`
FROM `pop_compare` AS `pop_compare`

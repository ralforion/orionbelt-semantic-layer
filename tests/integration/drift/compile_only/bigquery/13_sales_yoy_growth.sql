WITH `date_range` AS (
SELECT MIN(`__ob_pop_src`.`__ob_bucket`) AS min_date,
       MAX(`__ob_pop_src`.`__ob_bucket`) AS max_date
  FROM (
    SELECT DATE_TRUNC(`Sales`.`salesdate`, MONTH) AS `__ob_bucket`
      FROM `orionbelt_1`.`sales` AS `Sales`
  ) AS `__ob_pop_src`
),
`date_spine` AS (
SELECT d AS spine_date,
       CASE WHEN DATE_ADD(d, INTERVAL -1 YEAR) >= (SELECT min_date FROM `date_range`)
            THEN DATE_ADD(d, INTERVAL -1 YEAR) END AS spine_date_prev
FROM UNNEST(GENERATE_DATE_ARRAY((SELECT min_date FROM `date_range`), (SELECT max_date FROM `date_range`), INTERVAL 1 MONTH)) AS d
),
`pop_base` AS (
SELECT `date_spine`.spine_date AS `Sales Month`,
       ROUND(CAST(SUM(`__ob_pop_src`.`Sales__salesamount`) AS NUMERIC), 2) AS `Total Sales`
  FROM `date_spine`
  LEFT JOIN (
    SELECT DATE_TRUNC(`Sales`.`salesdate`, MONTH) AS `__ob_bucket`,
           `Sales`.`salesamount` AS `Sales__salesamount`
      FROM `orionbelt_1`.`sales` AS `Sales`
  ) AS `__ob_pop_src`
    ON `__ob_pop_src`.`__ob_bucket` = `date_spine`.spine_date
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
SELECT `Sales Month` AS `Sales Month`, ROUND(CAST(`Sales YoY Growth` AS NUMERIC), 4) AS `Sales YoY Growth`
FROM `pop_compare` AS `pop_compare`

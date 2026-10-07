WITH `date_range` AS (
SELECT MIN(`__ob_pop_src`.`__ob_bucket`) AS min_date,
       MAX(`__ob_pop_src`.`__ob_bucket`) AS max_date
  FROM (
    SELECT CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE) AS `__ob_bucket`
      FROM `orionbelt_1`.`sales` AS `Sales`
      WHERE `Sales`.`salesdate` >= '2021-03-01' AND `Sales`.`salesdate` < '2021-05-01'
  ) AS `__ob_pop_src`
),
`date_spine` AS (
SELECT spine_date,
       CASE WHEN DATE_SUB(spine_date, INTERVAL 1 MONTH) >= (SELECT min_date FROM `date_range`)
            THEN DATE_SUB(spine_date, INTERVAL 1 MONTH) END AS spine_date_prev
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
    SELECT CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE) AS `__ob_bucket`,
           `Sales`.`salesamount` AS `Sales__salesamount`
      FROM `orionbelt_1`.`sales` AS `Sales`
      WHERE `Sales`.`salesdate` >= '2021-03-01' AND `Sales`.`salesdate` < '2021-05-01'
  ) AS `__ob_pop_src`
    ON `__ob_pop_src`.`__ob_bucket` = `date_spine`.spine_date
  GROUP BY 1
),
`pop_lookback` AS (
SELECT `__ob_pop_src`.`__ob_bucket` AS `Sales Month`,
       CAST(SUM(`__ob_pop_src`.`Sales__salesamount`) AS DECIMAL(38, 2)) AS `Total Sales`
  FROM (
    SELECT CAST(DATE_FORMAT(`Sales`.`salesdate`, '%Y-%m-01') AS DATE) AS `__ob_bucket`,
           `Sales`.`salesamount` AS `Sales__salesamount`
      FROM `orionbelt_1`.`sales` AS `Sales`
  ) AS `__ob_pop_src`
  GROUP BY 1
),
`pop_compare` AS (
SELECT `pop_base`.`Sales Month` AS `Sales Month`,
       `pop_base`.`Total Sales` AS `Total Sales`,
       `pop_base`.`Total Sales` - pop_prev_0.`Total Sales` AS `Sales MoM Change`
  FROM `pop_base`
  LEFT JOIN `pop_lookback` AS pop_prev_0
    ON pop_prev_0.`Sales Month` = DATE_SUB(`pop_base`.`Sales Month`, INTERVAL 1 MONTH)
)
SELECT `Sales Month` AS `Sales Month`, `Total Sales` AS `Total Sales`, CAST(`Sales MoM Change` AS DECIMAL(38, 2)) AS `Sales MoM Change`
FROM `pop_compare` AS `pop_compare`

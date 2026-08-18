WITH `composite_01` AS (
SELECT `Returns`.`returnamount` AS `Total Returns`, CAST(NULL AS BIGNUMERIC) AS `Total Sales`
FROM `orionbelt_1`.`returns` AS `Returns`
UNION ALL
SELECT CAST(NULL AS BIGNUMERIC) AS `Total Returns`, `Sales`.`salesamount` AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
)
SELECT ROUND(CAST(SUM(`composite_01`.`Total Returns`) / NULLIF(NULLIF(SUM(`composite_01`.`Total Sales`), 0), 0) AS NUMERIC), 4) AS `Return Rate`
FROM `composite_01` AS `composite_01`

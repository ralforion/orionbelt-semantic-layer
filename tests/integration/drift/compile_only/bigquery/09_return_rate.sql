WITH `composite_01` AS (
SELECT CAST(`Returns`.`returnamount` AS NUMERIC) AS `Total Returns`, CAST(NULL AS NUMERIC) AS `Total Sales`
FROM ``.`orionbelt_1`.`returns` AS `Returns`
UNION ALL
SELECT CAST(NULL AS NUMERIC) AS `Total Returns`, CAST(`Sales`.`salesamount` AS NUMERIC) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
)
SELECT ROUND(CAST(SUM(`composite_01`.`Total Returns`) AS NUMERIC), 2) AS `Total Returns`, ROUND(CAST(SUM(`composite_01`.`Total Sales`) AS NUMERIC), 2) AS `Total Sales`, ROUND(CAST((SUM(`composite_01`.`Total Returns`) / SUM(`composite_01`.`Total Sales`)) AS NUMERIC), 4) AS `Return Rate`
FROM `composite_01` AS `composite_01`

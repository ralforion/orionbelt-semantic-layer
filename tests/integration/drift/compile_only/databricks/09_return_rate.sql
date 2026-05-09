WITH `composite_01` AS (
SELECT `Returns`.`returnamount` AS `Total Returns`, CAST(NULL AS DECIMAL(18, 2)) AS `Total Sales`
FROM ``.`orionbelt_1`.`returns` AS `Returns`
UNION ALL
SELECT CAST(NULL AS DECIMAL(18, 2)) AS `Total Returns`, `Sales`.`salesamount` AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
)
SELECT CAST(SUM(`composite_01`.`Total Returns`) AS DECIMAL(18, 2)) AS `Total Returns`, CAST(SUM(`composite_01`.`Total Sales`) AS DECIMAL(18, 2)) AS `Total Sales`, CAST((SUM(`composite_01`.`Total Returns`) / SUM(`composite_01`.`Total Sales`)) AS DECIMAL(18, 4)) AS `Return Rate`
FROM composite_01 AS `composite_01`

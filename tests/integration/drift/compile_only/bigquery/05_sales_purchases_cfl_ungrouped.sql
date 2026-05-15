WITH `composite_01` AS (
SELECT CAST(`Sales`.`salesamount` AS NUMERIC) AS `Total Sales`, CAST(NULL AS NUMERIC) AS `Total Purchases`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
UNION ALL
SELECT CAST(NULL AS NUMERIC) AS `Total Sales`, CAST(`Purchases`.`purchaseamount` AS NUMERIC) AS `Total Purchases`
FROM ``.`orionbelt_1`.`purchases` AS `Purchases`
)
SELECT ROUND(CAST(SUM(`composite_01`.`Total Sales`) AS NUMERIC), 2) AS `Total Sales`, ROUND(CAST(SUM(`composite_01`.`Total Purchases`) AS NUMERIC), 2) AS `Total Purchases`
FROM `composite_01` AS `composite_01`

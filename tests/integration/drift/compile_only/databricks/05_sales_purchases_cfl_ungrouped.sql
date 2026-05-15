WITH `composite_01` AS (
SELECT CAST(`Sales`.`salesamount` AS DECIMAL(18, 2)) AS `Total Sales`, CAST(NULL AS DECIMAL(18, 2)) AS `Total Purchases`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
UNION ALL
SELECT CAST(NULL AS DECIMAL(18, 2)) AS `Total Sales`, CAST(`Purchases`.`purchaseamount` AS DECIMAL(18, 2)) AS `Total Purchases`
FROM ``.`orionbelt_1`.`purchases` AS `Purchases`
)
SELECT CAST(SUM(`composite_01`.`Total Sales`) AS DECIMAL(18, 2)) AS `Total Sales`, CAST(SUM(`composite_01`.`Total Purchases`) AS DECIMAL(18, 2)) AS `Total Purchases`
FROM `composite_01` AS `composite_01`

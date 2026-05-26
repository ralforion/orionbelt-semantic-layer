WITH `composite_01` AS (
SELECT `Clients`.`clientname` AS `Sales Client Name`, CAST(NULL AS CHAR(255)) AS `Complaint Client Name`, CAST(`Sales`.`salesamount` AS DECIMAL(18, 2)) AS `Total Sales`, CAST(NULL AS CHAR(255)) AS `Complaint Count`
FROM `orionbelt_1`.`sales` AS `Sales`
LEFT JOIN `orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
UNION ALL
SELECT CAST(NULL AS CHAR(255)) AS `Sales Client Name`, `Clients`.`clientname` AS `Complaint Client Name`, CAST(NULL AS DECIMAL(18, 2)) AS `Total Sales`, CAST(`Client Complaints`.`complid` AS CHAR(255)) AS `Complaint Count`
FROM `orionbelt_1`.`clientcomplaints` AS `Client Complaints`
LEFT JOIN `orionbelt_1`.`clients` AS `Clients` ON `Client Complaints`.`complclientid` = `Clients`.`clientid`
)
SELECT COALESCE(`Sales Client Name`, `Complaint Client Name`) AS `Client`, CAST(SUM(`composite_01`.`Total Sales`) AS DECIMAL(18, 2)) AS `Total Sales`, CAST(COUNT(DISTINCT `composite_01`.`Complaint Count`) AS SIGNED) AS `Complaint Count`
FROM `composite_01` AS `composite_01`
GROUP BY COALESCE(`Sales Client Name`, `Complaint Client Name`)
HAVING CAST(COUNT(DISTINCT `composite_01`.`Complaint Count`) AS SIGNED) > 0

SELECT `Clients`.`clientname` AS `Sales Client Name`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
LEFT JOIN ``.`orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
GROUP BY `Clients`.`clientname`

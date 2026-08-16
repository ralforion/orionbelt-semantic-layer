SELECT CASE WHEN 2 > (CHAR_LENGTH(`Clients`.`clientemail`) - CHAR_LENGTH(REPLACE(`Clients`.`clientemail`, '@', ''))) / CHAR_LENGTH('@') + 1 THEN '' ELSE SUBSTRING_INDEX(SUBSTRING_INDEX(`Clients`.`clientemail`, '@', 2), '@', -1) END AS `Sales Client Email Domain`, CAST(SUM(`Sales`.`salesamount`) AS DECIMAL(18, 2)) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
LEFT JOIN `orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
GROUP BY CASE WHEN 2 > (CHAR_LENGTH(`Clients`.`clientemail`) - CHAR_LENGTH(REPLACE(`Clients`.`clientemail`, '@', ''))) / CHAR_LENGTH('@') + 1 THEN '' ELSE SUBSTRING_INDEX(SUBSTRING_INDEX(`Clients`.`clientemail`, '@', 2), '@', -1) END

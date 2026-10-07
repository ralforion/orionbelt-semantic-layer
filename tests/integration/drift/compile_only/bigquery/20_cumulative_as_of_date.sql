WITH `cumulative_base` AS (
SELECT `Regions`.`regionname` AS `Sales Region Name`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
LEFT JOIN `orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
LEFT JOIN `orionbelt_1`.`countries` AS `Countries` ON `Clients`.`clientcountryid` = `Countries`.`countryid`
LEFT JOIN `orionbelt_1`.`regions` AS `Regions` ON `Countries`.`region` = `Regions`.`regionid`
GROUP BY ALL
),
`cumulative_as_of_periods` AS (
SELECT `Regions`.`regionname` AS `Sales Region Name`, CAST(DATE_TRUNC(`Sales`.`salesdate`, MONTH) AS DATE) AS `Sales Month`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
LEFT JOIN `orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
LEFT JOIN `orionbelt_1`.`countries` AS `Countries` ON `Clients`.`clientcountryid` = `Countries`.`countryid`
LEFT JOIN `orionbelt_1`.`regions` AS `Regions` ON `Countries`.`region` = `Regions`.`regionid`
GROUP BY ALL
),
`cumulative_as_of` AS (
SELECT `cumulative_as_of_periods`.`Sales Region Name` AS `Sales Region Name`, SUM(CASE WHEN DATE_DIFF(CAST(CAST('2021-03-18' AS DATE) AS DATETIME), CAST(`cumulative_as_of_periods`.`Sales Month` AS DATETIME), YEAR) = 0 THEN `cumulative_as_of_periods`.`Total Sales` END) AS `YTD Sales`, SUM(`cumulative_as_of_periods`.`Total Sales`) AS `Cumulative Sales`
FROM `cumulative_as_of_periods` AS `cumulative_as_of_periods`
WHERE DATE_DIFF(CAST(CAST('2021-03-18' AS DATE) AS DATETIME), CAST(`cumulative_as_of_periods`.`Sales Month` AS DATETIME), MONTH) >= 0
GROUP BY ALL
),
`cumulative_as_of_periods_2` AS (
SELECT `Regions`.`regionname` AS `Sales Region Name`, CAST(DATE_TRUNC(`Sales`.`salesdate`, DAY) AS DATE) AS `Sales Date`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
LEFT JOIN `orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
LEFT JOIN `orionbelt_1`.`countries` AS `Countries` ON `Clients`.`clientcountryid` = `Countries`.`countryid`
LEFT JOIN `orionbelt_1`.`regions` AS `Regions` ON `Countries`.`region` = `Regions`.`regionid`
GROUP BY ALL
),
`cumulative_as_of_2` AS (
SELECT `cumulative_as_of_periods_2`.`Sales Region Name` AS `Sales Region Name`, SUM(CASE WHEN DATE_DIFF(CAST(CAST('2021-03-18' AS DATE) AS DATETIME), CAST(`cumulative_as_of_periods_2`.`Sales Date` AS DATETIME), MONTH) = 0 THEN `cumulative_as_of_periods_2`.`Total Sales` END) AS `MTD Sales`, AVG(CASE WHEN DATE_DIFF(CAST(CAST('2021-03-18' AS DATE) AS DATETIME), CAST(`cumulative_as_of_periods_2`.`Sales Date` AS DATETIME), DAY) <= 29 THEN `cumulative_as_of_periods_2`.`Total Sales` END) AS `Rolling 30 Day Sales`, MAX(CASE WHEN DATE_DIFF(CAST(CAST('2021-03-18' AS DATE) AS DATETIME), CAST(`cumulative_as_of_periods_2`.`Sales Date` AS DATETIME), DAY) <= 29 THEN `cumulative_as_of_periods_2`.`Total Sales` END) AS `Peak Daily Sales 30D`
FROM `cumulative_as_of_periods_2` AS `cumulative_as_of_periods_2`
WHERE DATE_DIFF(CAST(CAST('2021-03-18' AS DATE) AS DATETIME), CAST(`cumulative_as_of_periods_2`.`Sales Date` AS DATETIME), DAY) >= 0
GROUP BY ALL
),
`cumulative_joined` AS (
SELECT `cumulative_base`.`Sales Region Name` AS `Sales Region Name`, `cumulative_base`.`Total Sales` AS `Total Sales`, `cumulative_as_of`.`YTD Sales` AS `YTD Sales`, `cumulative_as_of`.`Cumulative Sales` AS `Cumulative Sales`, `cumulative_as_of_2`.`MTD Sales` AS `MTD Sales`, `cumulative_as_of_2`.`Rolling 30 Day Sales` AS `Rolling 30 Day Sales`, `cumulative_as_of_2`.`Peak Daily Sales 30D` AS `Peak Daily Sales 30D`
FROM `cumulative_base` AS `cumulative_base`
LEFT JOIN `cumulative_as_of` AS `cumulative_as_of` ON `cumulative_base`.`Sales Region Name` = `cumulative_as_of`.`Sales Region Name` OR `cumulative_base`.`Sales Region Name` IS NULL AND `cumulative_as_of`.`Sales Region Name` IS NULL
LEFT JOIN `cumulative_as_of_2` AS `cumulative_as_of_2` ON `cumulative_base`.`Sales Region Name` = `cumulative_as_of_2`.`Sales Region Name` OR `cumulative_base`.`Sales Region Name` IS NULL AND `cumulative_as_of_2`.`Sales Region Name` IS NULL
)
SELECT `Sales Region Name` AS `Sales Region Name`, `Total Sales` AS `Total Sales`, ROUND(CAST(`YTD Sales` AS NUMERIC), 2) AS `YTD Sales`, ROUND(CAST(`Cumulative Sales` AS NUMERIC), 2) AS `Cumulative Sales`, ROUND(CAST(`MTD Sales` AS NUMERIC), 2) AS `MTD Sales`, ROUND(CAST(`Rolling 30 Day Sales` AS NUMERIC), 0) AS `Rolling 30 Day Sales`, ROUND(CAST(`Peak Daily Sales 30D` AS NUMERIC), 2) AS `Peak Daily Sales 30D`
FROM `cumulative_joined` AS `cumulative_joined`

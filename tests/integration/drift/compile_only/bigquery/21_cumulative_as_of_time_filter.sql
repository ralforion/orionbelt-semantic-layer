WITH `cumulative_base` AS (
SELECT `Regions`.`regionname` AS `Sales Region Name`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
LEFT JOIN `orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
LEFT JOIN `orionbelt_1`.`countries` AS `Countries` ON `Clients`.`clientcountryid` = `Countries`.`countryid`
LEFT JOIN `orionbelt_1`.`regions` AS `Regions` ON `Countries`.`region` = `Regions`.`regionid`
WHERE `Sales`.`salesdate` >= '2022-03-01' AND `Sales`.`salesdate` < '2022-07-01'
GROUP BY ALL
),
`cumulative_as_of_shown` AS (
SELECT `Regions`.`regionname` AS `Sales Region Name`, CAST(DATE_TRUNC(`Sales`.`salesdate`, MONTH) AS DATE) AS `Sales Month`, ROUND(CAST(SUM(`Sales`.`salesamount`) AS NUMERIC), 2) AS `Total Sales`
FROM `orionbelt_1`.`sales` AS `Sales`
LEFT JOIN `orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
LEFT JOIN `orionbelt_1`.`countries` AS `Countries` ON `Clients`.`clientcountryid` = `Countries`.`countryid`
LEFT JOIN `orionbelt_1`.`regions` AS `Regions` ON `Countries`.`region` = `Regions`.`regionid`
WHERE `Sales`.`salesdate` >= '2022-03-01' AND `Sales`.`salesdate` < '2022-07-01'
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
`cumulative_as_of_point` AS (
SELECT MAX(`Sales Month`) AS `as_of`
FROM `cumulative_as_of_shown` AS `cumulative_as_of_shown`
),
`cumulative_as_of` AS (
SELECT `cumulative_as_of_periods`.`Sales Region Name` AS `Sales Region Name`, SUM(CASE WHEN DATE_DIFF(CAST(`cumulative_as_of_point`.`as_of` AS DATETIME), CAST(`cumulative_as_of_periods`.`Sales Month` AS DATETIME), YEAR) = 0 THEN `cumulative_as_of_periods`.`Total Sales` END) AS `YTD Sales`, SUM(`cumulative_as_of_periods`.`Total Sales`) AS `Cumulative Sales`
FROM `cumulative_as_of_periods` AS `cumulative_as_of_periods`
CROSS JOIN `cumulative_as_of_point` AS `cumulative_as_of_point`
WHERE DATE_DIFF(CAST(`cumulative_as_of_point`.`as_of` AS DATETIME), CAST(`cumulative_as_of_periods`.`Sales Month` AS DATETIME), MONTH) >= 0
GROUP BY ALL
),
`cumulative_joined` AS (
SELECT `cumulative_base`.`Sales Region Name` AS `Sales Region Name`, `cumulative_base`.`Total Sales` AS `Total Sales`, `cumulative_as_of`.`YTD Sales` AS `YTD Sales`, `cumulative_as_of`.`Cumulative Sales` AS `Cumulative Sales`
FROM `cumulative_base` AS `cumulative_base`
LEFT JOIN `cumulative_as_of` AS `cumulative_as_of` ON `cumulative_base`.`Sales Region Name` = `cumulative_as_of`.`Sales Region Name` OR `cumulative_base`.`Sales Region Name` IS NULL AND `cumulative_as_of`.`Sales Region Name` IS NULL
)
SELECT `Sales Region Name` AS `Sales Region Name`, `Total Sales` AS `Total Sales`, ROUND(CAST(`YTD Sales` AS NUMERIC), 2) AS `YTD Sales`, ROUND(CAST(`Cumulative Sales` AS NUMERIC), 2) AS `Cumulative Sales`
FROM `cumulative_joined` AS `cumulative_joined`

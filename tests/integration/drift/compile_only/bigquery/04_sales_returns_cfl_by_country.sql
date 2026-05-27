WITH `composite_01` AS (
SELECT `Countries`.`countryname` AS `Sales Country Name`, CAST(`Sales`.`salesamount` AS NUMERIC) AS `Total Sales`, CAST(NULL AS NUMERIC) AS `Total Returns`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
LEFT JOIN ``.`orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
LEFT JOIN ``.`orionbelt_1`.`countries` AS `Countries` ON `Clients`.`clientcountryid` = `Countries`.`countryid`
UNION ALL
SELECT `Countries`.`countryname` AS `Sales Country Name`, CAST(NULL AS NUMERIC) AS `Total Sales`, CAST(`Returns`.`returnamount` AS NUMERIC) AS `Total Returns`
FROM ``.`orionbelt_1`.`returns` AS `Returns`
LEFT JOIN ``.`orionbelt_1`.`sales` AS `Sales` ON `Returns`.`returnsalesid` = `Sales`.`salesid`
LEFT JOIN ``.`orionbelt_1`.`clients` AS `Clients` ON `Sales`.`salesclient` = `Clients`.`clientid`
LEFT JOIN ``.`orionbelt_1`.`countries` AS `Countries` ON `Clients`.`clientcountryid` = `Countries`.`countryid`
)
SELECT `Sales Country Name` AS `Sales Country Name`, ROUND(CAST(SUM(`composite_01`.`Total Sales`) AS NUMERIC), 2) AS `Total Sales`, ROUND(CAST(SUM(`composite_01`.`Total Returns`) AS NUMERIC), 2) AS `Total Returns`
FROM `composite_01` AS `composite_01`
GROUP BY ALL

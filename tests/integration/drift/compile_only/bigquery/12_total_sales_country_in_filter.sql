SELECT CAST(SUM(`Sales`.`salesamount`) AS NUMERIC(18, 2)) AS `Total Sales`
FROM ``.`orionbelt_1`.`sales` AS `Sales`
LEFT JOIN ``.`orionbelt_1`.`clients` AS `Clients` ON (`Sales`.`salesclient` = `Clients`.`clientid`)
LEFT JOIN ``.`orionbelt_1`.`countries` AS `Countries` ON (`Clients`.`clientcountryid` = `Countries`.`countryid`)
WHERE (`Countries`.`countryname` IN ('Croatia', 'Austria'))

-- dremio · datetime functions

-- date_trunc(unit, x)
--   date_trunc('month', DATE '2026-08-15') = '2026-08-01'
DATE_TRUNC('month', CAST('2026-08-15' AS DATE));
--   date_trunc('quarter', DATE '2026-08-15') = '2026-07-01'
DATE_TRUNC('quarter', CAST('2026-08-15' AS DATE));
--   date_trunc('week', DATE '2026-08-15') = '2026-08-10'
DATE_TRUNC('week', CAST('2026-08-15' AS DATE));
--   date_trunc('year', DATE '2026-08-15') = '2026-01-01'
DATE_TRUNC('year', CAST('2026-08-15' AS DATE));

-- date_add(unit, n, x)
--   date_add('day', 5, DATE '2026-08-01') = '2026-08-06'
TIMESTAMPADD(DAY, 5, CAST('2026-08-01' AS DATE));
--   date_add('day', -5, DATE '2026-08-01') = '2026-07-27'
TIMESTAMPADD(DAY, -5, CAST('2026-08-01' AS DATE));
--   date_add('month', 1, DATE '2026-01-31') = '2026-02-28'
TIMESTAMPADD(MONTH, 1, CAST('2026-01-31' AS DATE));
--   date_add('year', 1, DATE '2026-08-01') = '2027-08-01'
TIMESTAMPADD(YEAR, 1, CAST('2026-08-01' AS DATE));
--   date_add('quarter', 1 + 1, DATE '2026-01-01') = '2026-07-01'
TIMESTAMPADD(QUARTER, 1 + 1, CAST('2026-01-01' AS DATE));
--   date_add('week', 2, DATE '2026-08-01') = '2026-08-15'
TIMESTAMPADD(WEEK, 2, CAST('2026-08-01' AS DATE));
--   date_add('day', div(7, 2), DATE '2026-08-01') = '2026-08-04'
TIMESTAMPADD(DAY, CAST(SIGN((COALESCE(7, CAST(NULL AS DECIMAL(1, 1))) / NULLIF(2, 0))) * FLOOR(ABS((COALESCE(7, CAST(NULL AS DECIMAL(1, 1))) / NULLIF(2, 0)))) AS BIGINT), CAST('2026-08-01' AS DATE));

-- date_diff(unit, start, end)
--   date_diff('day', DATE '2026-08-01', DATE '2026-08-15') = 14
TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CAST('2026-08-01' AS DATE)), DATE_TRUNC('day', CAST('2026-08-15' AS DATE)));
--   date_diff('week', DATE '2026-08-09', DATE '2026-08-15') = 1
CAST(SIGN((COALESCE(TIMESTAMPDIFF(DAY, DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-09' AS DATE))), DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-15' AS DATE)))), CAST(NULL AS DECIMAL(1, 1))) / 7)) * FLOOR(ABS((COALESCE(TIMESTAMPDIFF(DAY, DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-09' AS DATE))), DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-15' AS DATE)))), CAST(NULL AS DECIMAL(1, 1))) / 7))) AS BIGINT);
--   date_diff('day', DATE '2026-08-15', DATE '2026-08-01') = -14
TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CAST('2026-08-15' AS DATE)), DATE_TRUNC('day', CAST('2026-08-01' AS DATE)));
--   date_diff('month', DATE '2026-01-31', DATE '2026-03-01') = 2
TIMESTAMPDIFF(MONTH, DATE_TRUNC('month', CAST('2026-01-31' AS DATE)), DATE_TRUNC('month', CAST('2026-03-01' AS DATE)));
--   date_diff('year', DATE '2026-12-31', DATE '2027-01-01') = 1
TIMESTAMPDIFF(YEAR, DATE_TRUNC('year', CAST('2026-12-31' AS DATE)), DATE_TRUNC('year', CAST('2027-01-01' AS DATE)));
--   date_diff('quarter', DATE '2026-03-31', DATE '2026-04-01') = 1
TIMESTAMPDIFF(QUARTER, DATE_TRUNC('quarter', CAST('2026-03-31' AS DATE)), DATE_TRUNC('quarter', CAST('2026-04-01' AS DATE)));
--   date_diff('day', TIMESTAMP '2026-01-31 23:00:00', TIMESTAMP '2026-02-01 01:00:00') = 1
TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CAST('2026-01-31 23:00:00' AS TIMESTAMP)), DATE_TRUNC('day', CAST('2026-02-01 01:00:00' AS TIMESTAMP)));
--   date_diff('hour', TIMESTAMP '2026-01-31 12:30:00', TIMESTAMP '2026-01-31 13:10:00') = 1
TIMESTAMPDIFF(HOUR, DATE_TRUNC('hour', CAST('2026-01-31 12:30:00' AS TIMESTAMP)), DATE_TRUNC('hour', CAST('2026-01-31 13:10:00' AS TIMESTAMP)));
--   date_diff('second', TIMESTAMP '2026-01-01 00:00:00.9', TIMESTAMP '2026-01-01 00:00:01.1') = 1
TIMESTAMPDIFF(SECOND, DATE_TRUNC('second', CAST('2026-01-01 00:00:00.9' AS TIMESTAMP)), DATE_TRUNC('second', CAST('2026-01-01 00:00:01.1' AS TIMESTAMP)));

-- extract(unit, x)
--   extract('year', DATE '2026-08-15') = 2026
EXTRACT(YEAR FROM CAST('2026-08-15' AS DATE));
--   extract('month', DATE '2026-08-15') = 8
EXTRACT(MONTH FROM CAST('2026-08-15' AS DATE));
--   extract('quarter', DATE '2026-08-15') = 3
EXTRACT(QUARTER FROM CAST('2026-08-15' AS DATE));
--   extract('week', DATE '2026-08-15') = 33
EXTRACT(WEEK FROM CAST('2026-08-15' AS DATE));
--   extract('day', DATE '2026-08-15') = 15
EXTRACT(DAY FROM CAST('2026-08-15' AS DATE));
--   extract('day_of_week', DATE '2026-08-15') = 6
(MOD(EXTRACT(DOW FROM CAST('2026-08-15' AS DATE)) + 5, 7) + 1);
--   extract('day_of_week', DATE '2026-08-16') = 7
(MOD(EXTRACT(DOW FROM CAST('2026-08-16' AS DATE)) + 5, 7) + 1);
--   extract('day_of_week', DATE '2026-08-10') = 1
(MOD(EXTRACT(DOW FROM CAST('2026-08-10' AS DATE)) + 5, 7) + 1);

-- last_day(x)
--   last_day(DATE '2026-08-15') = '2026-08-31'
LAST_DAY(CAST('2026-08-15' AS DATE));
--   last_day(DATE '2026-02-01') = '2026-02-28'
LAST_DAY(CAST('2026-02-01' AS DATE));

-- current_date()
--   date_diff('day', current_date(), current_date()) = 0
TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CURRENT_DATE()), DATE_TRUNC('day', CURRENT_DATE()));

-- current_timestamp()
--   date_diff('day', current_timestamp(), current_timestamp()) = 0
TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CURRENT_TIMESTAMP), DATE_TRUNC('day', CURRENT_TIMESTAMP));

-- current_time()
--   CASE WHEN current_time() IS NULL THEN 0 ELSE 1 END = 1
CASE WHEN CAST(CURRENT_TIMESTAMP AS TIME) IS NULL THEN 0 ELSE 1 END;

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

-- date_diff(unit, start, end)
--   date_diff('day', DATE '2026-08-01', DATE '2026-08-15') = 14
TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CAST('2026-08-01' AS DATE)), DATE_TRUNC('day', CAST('2026-08-15' AS DATE)));
--   date_diff('week', DATE '2026-08-09', DATE '2026-08-15') = 1
(SIGN((TIMESTAMPDIFF(DAY, DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-09' AS DATE))), DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-15' AS DATE)))) * 1.0 / 7)) * FLOOR(ABS((TIMESTAMPDIFF(DAY, DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-09' AS DATE))), DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-15' AS DATE)))) * 1.0 / 7))));
--   date_diff('day', DATE '2026-08-15', DATE '2026-08-01') = -14
TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CAST('2026-08-15' AS DATE)), DATE_TRUNC('day', CAST('2026-08-01' AS DATE)));
--   date_diff('month', DATE '2026-01-31', DATE '2026-03-01') = 2
TIMESTAMPDIFF(MONTH, DATE_TRUNC('month', CAST('2026-01-31' AS DATE)), DATE_TRUNC('month', CAST('2026-03-01' AS DATE)));
--   date_diff('year', DATE '2026-12-31', DATE '2027-01-01') = 1
TIMESTAMPDIFF(YEAR, DATE_TRUNC('year', CAST('2026-12-31' AS DATE)), DATE_TRUNC('year', CAST('2027-01-01' AS DATE)));

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

-- last_day(x)
--   last_day(DATE '2026-08-15') = '2026-08-31'
LAST_DAY(CAST('2026-08-15' AS DATE));
--   last_day(DATE '2026-02-01') = '2026-02-28'
LAST_DAY(CAST('2026-02-01' AS DATE));

-- current_date()
--   date_diff('day', current_date(), current_date()) = 0
TIMESTAMPDIFF(DAY, DATE_TRUNC('day', CURRENT_DATE()), DATE_TRUNC('day', CURRENT_DATE()));

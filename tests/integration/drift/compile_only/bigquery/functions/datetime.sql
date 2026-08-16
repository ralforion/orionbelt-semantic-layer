-- bigquery · datetime functions

-- date_trunc(unit, x)
--   date_trunc('month', DATE '2026-08-15') = '2026-08-01'
DATE_TRUNC(CAST('2026-08-15' AS DATE), MONTH);
--   date_trunc('quarter', DATE '2026-08-15') = '2026-07-01'
DATE_TRUNC(CAST('2026-08-15' AS DATE), QUARTER);
--   date_trunc('week', DATE '2026-08-15') = '2026-08-10'
DATE_TRUNC(CAST('2026-08-15' AS DATE), ISOWEEK);
--   date_trunc('year', DATE '2026-08-15') = '2026-01-01'
DATE_TRUNC(CAST('2026-08-15' AS DATE), YEAR);

-- date_add(unit, n, x)
--   date_add('day', 5, DATE '2026-08-01') = '2026-08-06'
(CAST('2026-08-01' AS DATE) + INTERVAL 5 DAY);
--   date_add('day', -5, DATE '2026-08-01') = '2026-07-27'
(CAST('2026-08-01' AS DATE) + INTERVAL -5 DAY);
--   date_add('month', 1, DATE '2026-01-31') = '2026-02-28'
(CAST('2026-01-31' AS DATE) + INTERVAL 1 MONTH);
--   date_add('year', 1, DATE '2026-08-01') = '2027-08-01'
(CAST('2026-08-01' AS DATE) + INTERVAL 1 YEAR);

-- date_diff(unit, start, end)
--   date_diff('day', DATE '2026-08-01', DATE '2026-08-15') = 14
DATE_DIFF(CAST('2026-08-15' AS DATE), CAST('2026-08-01' AS DATE), DAY);
--   date_diff('week', DATE '2026-08-09', DATE '2026-08-15') = 1
DIV(DATE_DIFF(DATE_TRUNC(CAST('2026-08-15' AS DATE), ISOWEEK), DATE_TRUNC(CAST('2026-08-09' AS DATE), ISOWEEK), DAY), 7);
--   date_diff('day', DATE '2026-08-15', DATE '2026-08-01') = -14
DATE_DIFF(CAST('2026-08-01' AS DATE), CAST('2026-08-15' AS DATE), DAY);
--   date_diff('month', DATE '2026-01-31', DATE '2026-03-01') = 2
DATE_DIFF(CAST('2026-03-01' AS DATE), CAST('2026-01-31' AS DATE), MONTH);
--   date_diff('year', DATE '2026-12-31', DATE '2027-01-01') = 1
DATE_DIFF(CAST('2027-01-01' AS DATE), CAST('2026-12-31' AS DATE), YEAR);

-- extract(unit, x)
--   extract('year', DATE '2026-08-15') = 2026
EXTRACT(YEAR FROM CAST('2026-08-15' AS DATE));
--   extract('month', DATE '2026-08-15') = 8
EXTRACT(MONTH FROM CAST('2026-08-15' AS DATE));
--   extract('quarter', DATE '2026-08-15') = 3
EXTRACT(QUARTER FROM CAST('2026-08-15' AS DATE));
--   extract('week', DATE '2026-08-15') = 33
EXTRACT(ISOWEEK FROM CAST('2026-08-15' AS DATE));
--   extract('day', DATE '2026-08-15') = 15
EXTRACT(DAY FROM CAST('2026-08-15' AS DATE));

-- last_day(x)
--   last_day(DATE '2026-08-15') = '2026-08-31'
LAST_DAY(CAST('2026-08-15' AS DATE));
--   last_day(DATE '2026-02-01') = '2026-02-28'
LAST_DAY(CAST('2026-02-01' AS DATE));

-- current_date()
--   date_diff('day', current_date(), current_date()) = 0
DATE_DIFF(CURRENT_DATE(), CURRENT_DATE(), DAY);

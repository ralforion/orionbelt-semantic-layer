-- clickhouse · datetime functions

-- date_trunc(unit, x)
--   date_trunc('month', DATE '2026-08-15') = '2026-08-01'
DATE_TRUNC('month', CAST('2026-08-15' AS Nullable(Date)));
--   date_trunc('quarter', DATE '2026-08-15') = '2026-07-01'
DATE_TRUNC('quarter', CAST('2026-08-15' AS Nullable(Date)));
--   date_trunc('week', DATE '2026-08-15') = '2026-08-10'
DATE_TRUNC('week', CAST('2026-08-15' AS Nullable(Date)));
--   date_trunc('year', DATE '2026-08-15') = '2026-01-01'
DATE_TRUNC('year', CAST('2026-08-15' AS Nullable(Date)));

-- date_add(unit, n, x)
--   date_add('day', 5, DATE '2026-08-01') = '2026-08-06'
date_add(DAY, 5, CAST('2026-08-01' AS Nullable(Date)));
--   date_add('day', -5, DATE '2026-08-01') = '2026-07-27'
date_add(DAY, -5, CAST('2026-08-01' AS Nullable(Date)));
--   date_add('month', 1, DATE '2026-01-31') = '2026-02-28'
date_add(MONTH, 1, CAST('2026-01-31' AS Nullable(Date)));
--   date_add('year', 1, DATE '2026-08-01') = '2027-08-01'
date_add(YEAR, 1, CAST('2026-08-01' AS Nullable(Date)));
--   date_add('quarter', 1 + 1, DATE '2026-01-01') = '2026-07-01'
date_add(QUARTER, 1 + 1, CAST('2026-01-01' AS Nullable(Date)));

-- date_diff(unit, start, end)
--   date_diff('day', DATE '2026-08-01', DATE '2026-08-15') = 14
date_diff('day', CAST('2026-08-01' AS Nullable(Date)), CAST('2026-08-15' AS Nullable(Date)));
--   date_diff('week', DATE '2026-08-09', DATE '2026-08-15') = 1
intDiv(date_diff('day', DATE_TRUNC('week', CAST('2026-08-09' AS Nullable(Date))), DATE_TRUNC('week', CAST('2026-08-15' AS Nullable(Date)))), 7);
--   date_diff('day', DATE '2026-08-15', DATE '2026-08-01') = -14
date_diff('day', CAST('2026-08-15' AS Nullable(Date)), CAST('2026-08-01' AS Nullable(Date)));
--   date_diff('month', DATE '2026-01-31', DATE '2026-03-01') = 2
date_diff('month', CAST('2026-01-31' AS Nullable(Date)), CAST('2026-03-01' AS Nullable(Date)));
--   date_diff('year', DATE '2026-12-31', DATE '2027-01-01') = 1
date_diff('year', CAST('2026-12-31' AS Nullable(Date)), CAST('2027-01-01' AS Nullable(Date)));
--   date_diff('quarter', DATE '2026-03-31', DATE '2026-04-01') = 1
date_diff('quarter', CAST('2026-03-31' AS Nullable(Date)), CAST('2026-04-01' AS Nullable(Date)));

-- extract(unit, x)
--   extract('year', DATE '2026-08-15') = 2026
EXTRACT(YEAR FROM CAST('2026-08-15' AS Nullable(Date)));
--   extract('month', DATE '2026-08-15') = 8
EXTRACT(MONTH FROM CAST('2026-08-15' AS Nullable(Date)));
--   extract('quarter', DATE '2026-08-15') = 3
EXTRACT(QUARTER FROM CAST('2026-08-15' AS Nullable(Date)));
--   extract('week', DATE '2026-08-15') = 33
EXTRACT(WEEK FROM CAST('2026-08-15' AS Nullable(Date)));
--   extract('day', DATE '2026-08-15') = 15
EXTRACT(DAY FROM CAST('2026-08-15' AS Nullable(Date)));

-- last_day(x)
--   last_day(DATE '2026-08-15') = '2026-08-31'
LAST_DAY(CAST('2026-08-15' AS Nullable(Date)));
--   last_day(DATE '2026-02-01') = '2026-02-28'
LAST_DAY(CAST('2026-02-01' AS Nullable(Date)));

-- current_date()
--   date_diff('day', current_date(), current_date()) = 0
date_diff('day', CURRENT_DATE(), CURRENT_DATE());

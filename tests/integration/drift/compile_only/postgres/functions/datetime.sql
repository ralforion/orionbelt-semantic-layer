-- postgres · datetime functions

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
(CAST('2026-08-01' AS DATE) + 5 * INTERVAL '1 day');
--   date_add('day', -5, DATE '2026-08-01') = '2026-07-27'
(CAST('2026-08-01' AS DATE) + -5 * INTERVAL '1 day');
--   date_add('month', 1, DATE '2026-01-31') = '2026-02-28'
(CAST('2026-01-31' AS DATE) + 1 * INTERVAL '1 month');
--   date_add('year', 1, DATE '2026-08-01') = '2027-08-01'
(CAST('2026-08-01' AS DATE) + 1 * INTERVAL '1 year');

-- date_diff(unit, start, end)
--   date_diff('day', DATE '2026-08-01', DATE '2026-08-15') = 14
CAST(TRUNC(EXTRACT(EPOCH FROM (DATE_TRUNC('day', CAST('2026-08-15' AS DATE)) - DATE_TRUNC('day', CAST('2026-08-01' AS DATE)))) / 86400) AS INTEGER);
--   date_diff('week', DATE '2026-08-09', DATE '2026-08-15') = 1
DIV(CAST(TRUNC(EXTRACT(EPOCH FROM (DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-15' AS DATE))) - DATE_TRUNC('day', DATE_TRUNC('week', CAST('2026-08-09' AS DATE))))) / 86400) AS INTEGER), 7);
--   date_diff('day', DATE '2026-08-15', DATE '2026-08-01') = -14
CAST(TRUNC(EXTRACT(EPOCH FROM (DATE_TRUNC('day', CAST('2026-08-01' AS DATE)) - DATE_TRUNC('day', CAST('2026-08-15' AS DATE)))) / 86400) AS INTEGER);
--   date_diff('month', DATE '2026-01-31', DATE '2026-03-01') = 2
CAST(TRUNC((EXTRACT(YEAR FROM DATE_TRUNC('month', CAST('2026-03-01' AS DATE))) - EXTRACT(YEAR FROM DATE_TRUNC('month', CAST('2026-01-31' AS DATE)))) * 12 + (EXTRACT(MONTH FROM DATE_TRUNC('month', CAST('2026-03-01' AS DATE))) - EXTRACT(MONTH FROM DATE_TRUNC('month', CAST('2026-01-31' AS DATE))))) AS INTEGER);
--   date_diff('year', DATE '2026-12-31', DATE '2027-01-01') = 1
CAST(TRUNC(((EXTRACT(YEAR FROM DATE_TRUNC('year', CAST('2027-01-01' AS DATE))) - EXTRACT(YEAR FROM DATE_TRUNC('year', CAST('2026-12-31' AS DATE)))) * 12 + (EXTRACT(MONTH FROM DATE_TRUNC('year', CAST('2027-01-01' AS DATE))) - EXTRACT(MONTH FROM DATE_TRUNC('year', CAST('2026-12-31' AS DATE))))) / 12) AS INTEGER);

-- extract(unit, x)
--   extract('year', DATE '2026-08-15') = 2026
CAST(EXTRACT(YEAR FROM CAST('2026-08-15' AS DATE)) AS INTEGER);
--   extract('month', DATE '2026-08-15') = 8
CAST(EXTRACT(MONTH FROM CAST('2026-08-15' AS DATE)) AS INTEGER);
--   extract('quarter', DATE '2026-08-15') = 3
CAST(EXTRACT(QUARTER FROM CAST('2026-08-15' AS DATE)) AS INTEGER);
--   extract('week', DATE '2026-08-15') = 33
CAST(EXTRACT(WEEK FROM CAST('2026-08-15' AS DATE)) AS INTEGER);
--   extract('day', DATE '2026-08-15') = 15
CAST(EXTRACT(DAY FROM CAST('2026-08-15' AS DATE)) AS INTEGER);

-- last_day(x)
--   last_day(DATE '2026-08-15') = '2026-08-31'
CAST(DATE_TRUNC('month', CAST('2026-08-15' AS DATE)) + INTERVAL '1 month' - INTERVAL '1 day' AS DATE);
--   last_day(DATE '2026-02-01') = '2026-02-28'
CAST(DATE_TRUNC('month', CAST('2026-02-01' AS DATE)) + INTERVAL '1 month' - INTERVAL '1 day' AS DATE);

-- current_date()
--   date_diff('day', current_date(), current_date()) = 0
CAST(TRUNC(EXTRACT(EPOCH FROM (DATE_TRUNC('day', CURRENT_DATE) - DATE_TRUNC('day', CURRENT_DATE))) / 86400) AS INTEGER);

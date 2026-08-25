-- databricks · numeric functions

-- abs(x)
--   abs(-3) = 3
ABS(-3);

-- sign(x)
--   sign(-3) = -1
SIGN(-3);

-- floor(x)
--   floor(-1.2) = -2
FLOOR(-1.2);

-- ceil(x)
--   ceil(1.2) = 2
CEIL(1.2);

-- sqrt(x)
--   sqrt(4) = 2
SQRT(4);

-- ln(x)
--   ln(1) = 0
LN(1);

-- exp(x)
--   exp(0) = 1
EXP(0);

-- power(base, exponent)
--   power(2, 10) = 1024
POWER(2, 10);

-- cast(x, 'type')
--   cast('4.60', 'double') = 4.6
CAST('4.60' AS DOUBLE);
--   cast(2.555, 'decimal(18, 2)') = 2.56
CAST(2.555 AS DECIMAL(18, 2));

-- round(x, n?)
--   round(2.5) = 3
ROUND(2.5);
--   round(-2.5) = -3
ROUND(-2.5);
--   round(0.5) = 1
ROUND(0.5);
--   round(2.345, 2) = 2.35
ROUND(2.345, 2);

-- trunc(x, n?)
--   trunc(1.9) = 1
(SIGN(1.9) * FLOOR(ABS(1.9)));
--   trunc(-1.9) = -1
(SIGN(-1.9) * FLOOR(ABS(-1.9)));
--   trunc(2.345, 2) = 2.34
(SIGN(2.345) * FLOOR(ABS(2.345) * POWER(10, 2)) / POWER(10, 2));

-- mod(a, b)
--   mod(7, 3) = 1
MOD(7, 3);
--   mod(-7, 3) = -1
MOD(-7, 3);

-- div(a, b)
--   div(7, 2) = 3
(7 div NULLIF(2, 0));
--   div(-7, 2) = -3
(-7 div NULLIF(2, 0));
--   div(7, 0) = None
(7 div NULLIF(0, 0));

-- log(base, x)
--   log(10, 100) = 2
(CASE WHEN 10 <= 0 OR 10 = 1 OR 100 <= 0 THEN NULL ELSE LOG(10, 100) END);
--   log(2, 8) = 3
(CASE WHEN 2 <= 0 OR 2 = 1 OR 8 <= 0 THEN NULL ELSE LOG(2, 8) END);
--   log(1, 8) = None
(CASE WHEN 1 <= 0 OR 1 = 1 OR 8 <= 0 THEN NULL ELSE LOG(1, 8) END);
--   log(0, 8) = None
(CASE WHEN 0 <= 0 OR 0 = 1 OR 8 <= 0 THEN NULL ELSE LOG(0, 8) END);
--   log(2, 0) = None
(CASE WHEN 2 <= 0 OR 2 = 1 OR 0 <= 0 THEN NULL ELSE LOG(2, 0) END);
--   log(2, -8) = None
(CASE WHEN 2 <= 0 OR 2 = 1 OR -8 <= 0 THEN NULL ELSE LOG(2, -8) END);

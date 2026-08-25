-- bigquery · numeric functions

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

-- to_number(x)
--   to_number('4.6') = 4.6
CASE WHEN REGEXP_CONTAINS(TRIM(CAST('4.6' AS STRING)), '^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$') THEN SAFE_CAST(TRIM(CAST('4.6' AS STRING)) AS FLOAT64) END;
--   to_number('abc') = None
CASE WHEN REGEXP_CONTAINS(TRIM(CAST('abc' AS STRING)), '^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$') THEN SAFE_CAST(TRIM(CAST('abc' AS STRING)) AS FLOAT64) END;

-- cast(x, 'type')
--   cast('4.60', 'double') = 4.6
CAST('4.60' AS FLOAT64);
--   cast(2.555, 'decimal(18, 2)') = 2.56
ROUND(CAST(2.555 AS NUMERIC), 2);

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
TRUNC(1.9);
--   trunc(-1.9) = -1
TRUNC(-1.9);
--   trunc(2.345, 2) = 2.34
TRUNC(2.345, 2);

-- mod(a, b)
--   mod(7, 3) = 1
MOD(7, 3);
--   mod(-7, 3) = -1
MOD(-7, 3);

-- div(a, b)
--   div(7, 2) = 3
DIV(7, NULLIF(2, 0));
--   div(-7, 2) = -3
DIV(-7, NULLIF(2, 0));
--   div(7, 0) = None
DIV(7, NULLIF(0, 0));

-- log(base, x)
--   log(10, 100) = 2
(CASE WHEN 10 <= 0 OR 10 = 1 OR 100 <= 0 THEN NULL ELSE LOG(100, 10) END);
--   log(2, 8) = 3
(CASE WHEN 2 <= 0 OR 2 = 1 OR 8 <= 0 THEN NULL ELSE LOG(8, 2) END);
--   log(1, 8) = None
(CASE WHEN 1 <= 0 OR 1 = 1 OR 8 <= 0 THEN NULL ELSE LOG(8, 1) END);
--   log(0, 8) = None
(CASE WHEN 0 <= 0 OR 0 = 1 OR 8 <= 0 THEN NULL ELSE LOG(8, 0) END);
--   log(2, 0) = None
(CASE WHEN 2 <= 0 OR 2 = 1 OR 0 <= 0 THEN NULL ELSE LOG(0, 2) END);
--   log(2, -8) = None
(CASE WHEN 2 <= 0 OR 2 = 1 OR -8 <= 0 THEN NULL ELSE LOG(-8, 2) END);

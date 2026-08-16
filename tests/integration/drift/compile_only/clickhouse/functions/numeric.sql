-- clickhouse · numeric functions

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

-- round(x, n?)
--   round(2.5) = 3
(sign(2.5) * floor(abs(2.5) + 0.5));
--   round(-2.5) = -3
(sign(-2.5) * floor(abs(-2.5) + 0.5));
--   round(0.5) = 1
(sign(0.5) * floor(abs(0.5) + 0.5));
--   round(2.345, 2) = 2.35
(sign(2.345) * floor(abs(2.345) * pow(10, 2) + 0.5) / pow(10, 2));

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
intDiv(7, 2);
--   div(-7, 2) = -3
intDiv(-7, 2);

-- log(base, x)
--   log(10, 100) = 2
(log10(100) / log10(10));
--   log(2, 8) = 3
(log10(8) / log10(2));

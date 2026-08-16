-- clickhouse · string functions

-- substring(x, start, len?)
--   substring('abcdef', 2, 3) = 'bcd'
SUBSTRING('abcdef', 2, 3);
--   substring('abcdef', 2) = 'bcdef'
SUBSTRING('abcdef', 2);

-- concat(a, b, ...)
--   concat('a', 'b', 'c') = 'abc'
CONCAT('a', 'b', 'c');
--   concat('a', NULL, 'c') = None
CONCAT('a', NULL, 'c');

-- upper(x)
--   upper('aBc') = 'ABC'
UPPER('aBc');

-- lower(x)
--   lower('AbC') = 'abc'
LOWER('AbC');

-- trim(x)
--   trim('  ab  ') = 'ab'
TRIM('  ab  ');

-- ltrim(x)
--   ltrim('  ab') = 'ab'
LTRIM('  ab');

-- rtrim(x)
--   rtrim('ab  ') = 'ab'
RTRIM('ab  ');

-- length(x)
--   length('äbcd') = 4
lengthUTF8('äbcd');

-- replace(x, from, to)
--   replace('abcab', 'ab', 'X') = 'XcX'
REPLACE('abcab', 'ab', 'X');

-- position(needle, haystack)
--   position('cd', 'abcd') = 3
POSITION('cd' IN 'abcd');
--   position('zz', 'abcd') = 0
POSITION('zz' IN 'abcd');

-- split_part(x, delim, n)
--   split_part('a,b,c', ',', 2) = 'b'
splitByString(',', 'a,b,c')[2];
--   split_part('a,b,c', ',', 9) = ''
splitByString(',', 'a,b,c')[9];

-- lpad(x, len, fill)
--   lpad('7', 3, '0') = '007'
LPAD('7', 3, '0');

-- rpad(x, len, fill)
--   rpad('7', 3, '0') = '700'
RPAD('7', 3, '0');

-- starts_with(x, prefix)
--   starts_with('abcd', 'ab') = True
startsWith('abcd', 'ab');
--   starts_with('abcd', 'bc') = False
startsWith('abcd', 'bc');

-- ends_with(x, suffix)
--   ends_with('abcd', 'cd') = True
endsWith('abcd', 'cd');
--   ends_with('abcd', 'bc') = False
endsWith('abcd', 'bc');

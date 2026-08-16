-- bigquery · conditional functions

-- coalesce(a, b, ...)
--   coalesce(NULL, 'x') = 'x'
COALESCE(NULL, 'x');
--   coalesce(NULL, NULL) = None
COALESCE(NULL, NULL);

-- nullif(a, b)
--   nullif('a', 'a') = None
NULLIF('a', 'a');
--   nullif('a', 'b') = 'a'
NULLIF('a', 'b');

-- greatest(a, b, ...)
--   greatest(1, 2, 3) = 3
GREATEST(1, 2, 3);
--   greatest(1, NULL, 3) = None
GREATEST(1, NULL, 3);

-- least(a, b, ...)
--   least(3, 2, 1) = 1
LEAST(3, 2, 1);
--   least(3, NULL, 1) = None
LEAST(3, NULL, 1);

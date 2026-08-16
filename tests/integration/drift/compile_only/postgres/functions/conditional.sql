-- postgres · conditional functions

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
CASE WHEN 1 IS NULL OR 2 IS NULL OR 3 IS NULL THEN NULL ELSE GREATEST(1, 2, 3) END;
--   greatest(1, NULL, 3) = None
CASE WHEN 1 IS NULL OR NULL IS NULL OR 3 IS NULL THEN NULL ELSE GREATEST(1, NULL, 3) END;

-- least(a, b, ...)
--   least(3, 2, 1) = 1
CASE WHEN 3 IS NULL OR 2 IS NULL OR 1 IS NULL THEN NULL ELSE LEAST(3, 2, 1) END;
--   least(3, NULL, 1) = None
CASE WHEN 3 IS NULL OR NULL IS NULL OR 1 IS NULL THEN NULL ELSE LEAST(3, NULL, 1) END;

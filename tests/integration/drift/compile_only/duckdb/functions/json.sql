-- duckdb · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
CASE WHEN json_type('{"a": "x"}', '$.a') IN ('OBJECT', 'ARRAY') THEN NULL ELSE json_extract_string('{"a": "x"}', '$.a') END;
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
CASE WHEN json_type('{"o": {"b": "y"}}', '$.o.b') IN ('OBJECT', 'ARRAY') THEN NULL ELSE json_extract_string('{"o": {"b": "y"}}', '$.o.b') END;
--   json_value('{"n": 1}', '$.n') = '1'
CASE WHEN json_type('{"n": 1}', '$.n') IN ('OBJECT', 'ARRAY') THEN NULL ELSE json_extract_string('{"n": 1}', '$.n') END;
--   json_value('{"a": "x"}', '$.zz') = None
CASE WHEN json_type('{"a": "x"}', '$.zz') IN ('OBJECT', 'ARRAY') THEN NULL ELSE json_extract_string('{"a": "x"}', '$.zz') END;
--   json_value('{"o": {"b": "y"}}', '$.o') = None
CASE WHEN json_type('{"o": {"b": "y"}}', '$.o') IN ('OBJECT', 'ARRAY') THEN NULL ELSE json_extract_string('{"o": {"b": "y"}}', '$.o') END;
--   json_value('{"arr": ["z"]}', '$.arr') = None
CASE WHEN json_type('{"arr": ["z"]}', '$.arr') IN ('OBJECT', 'ARRAY') THEN NULL ELSE json_extract_string('{"arr": ["z"]}', '$.arr') END;
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
CASE WHEN json_type('{"arr": ["z"]}', '$.arr[0]') IN ('OBJECT', 'ARRAY') THEN NULL ELSE json_extract_string('{"arr": ["z"]}', '$.arr[0]') END;

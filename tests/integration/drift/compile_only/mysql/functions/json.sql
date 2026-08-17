-- mysql · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
CASE WHEN JSON_TYPE(JSON_EXTRACT('{"a": "x"}', '$.a')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT('{"a": "x"}', '$.a')) END;
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
CASE WHEN JSON_TYPE(JSON_EXTRACT('{"o": {"b": "y"}}', '$.o.b')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT('{"o": {"b": "y"}}', '$.o.b')) END;
--   json_value('{"n": 1}', '$.n') = '1'
CASE WHEN JSON_TYPE(JSON_EXTRACT('{"n": 1}', '$.n')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT('{"n": 1}', '$.n')) END;
--   json_value('{"a": "x"}', '$.zz') = None
CASE WHEN JSON_TYPE(JSON_EXTRACT('{"a": "x"}', '$.zz')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT('{"a": "x"}', '$.zz')) END;
--   json_value('{"o": {"b": "y"}}', '$.o') = None
CASE WHEN JSON_TYPE(JSON_EXTRACT('{"o": {"b": "y"}}', '$.o')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT('{"o": {"b": "y"}}', '$.o')) END;
--   json_value('{"arr": ["z"]}', '$.arr') = None
CASE WHEN JSON_TYPE(JSON_EXTRACT('{"arr": ["z"]}', '$.arr')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT('{"arr": ["z"]}', '$.arr')) END;
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
CASE WHEN JSON_TYPE(JSON_EXTRACT('{"arr": ["z"]}', '$.arr[0]')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT('{"arr": ["z"]}', '$.arr[0]')) END;
--   json_value('{"select": "x"}', '$.select') = 'x'
CASE WHEN JSON_TYPE(JSON_EXTRACT('{"select": "x"}', '$.select')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT('{"select": "x"}', '$.select')) END;

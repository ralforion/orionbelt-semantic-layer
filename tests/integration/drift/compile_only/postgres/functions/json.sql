-- postgres · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
CASE WHEN json_typeof(json_extract_path('{"a": "x"}'::json, 'a')) IN ('object', 'array') THEN NULL ELSE json_extract_path_text('{"a": "x"}'::json, 'a') END;
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
CASE WHEN json_typeof(json_extract_path('{"o": {"b": "y"}}'::json, 'o', 'b')) IN ('object', 'array') THEN NULL ELSE json_extract_path_text('{"o": {"b": "y"}}'::json, 'o', 'b') END;
--   json_value('{"n": 1}', '$.n') = '1'
CASE WHEN json_typeof(json_extract_path('{"n": 1}'::json, 'n')) IN ('object', 'array') THEN NULL ELSE json_extract_path_text('{"n": 1}'::json, 'n') END;
--   json_value('{"a": "x"}', '$.zz') = None
CASE WHEN json_typeof(json_extract_path('{"a": "x"}'::json, 'zz')) IN ('object', 'array') THEN NULL ELSE json_extract_path_text('{"a": "x"}'::json, 'zz') END;
--   json_value('{"o": {"b": "y"}}', '$.o') = None
CASE WHEN json_typeof(json_extract_path('{"o": {"b": "y"}}'::json, 'o')) IN ('object', 'array') THEN NULL ELSE json_extract_path_text('{"o": {"b": "y"}}'::json, 'o') END;
--   json_value('{"arr": ["z"]}', '$.arr') = None
CASE WHEN json_typeof(json_extract_path('{"arr": ["z"]}'::json, 'arr')) IN ('object', 'array') THEN NULL ELSE json_extract_path_text('{"arr": ["z"]}'::json, 'arr') END;
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
CASE WHEN json_typeof(json_extract_path('{"arr": ["z"]}'::json, 'arr', '0')) IN ('object', 'array') THEN NULL ELSE json_extract_path_text('{"arr": ["z"]}'::json, 'arr', '0') END;

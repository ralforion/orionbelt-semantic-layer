-- snowflake · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
CASE WHEN TYPEOF(GET_PATH(PARSE_JSON('{"a": "x"}'), 'a')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT('{"a": "x"}', 'a') END;
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
CASE WHEN TYPEOF(GET_PATH(PARSE_JSON('{"o": {"b": "y"}}'), 'o.b')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT('{"o": {"b": "y"}}', 'o.b') END;
--   json_value('{"n": 1}', '$.n') = '1'
CASE WHEN TYPEOF(GET_PATH(PARSE_JSON('{"n": 1}'), 'n')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT('{"n": 1}', 'n') END;
--   json_value('{"a": "x"}', '$.zz') = None
CASE WHEN TYPEOF(GET_PATH(PARSE_JSON('{"a": "x"}'), 'zz')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT('{"a": "x"}', 'zz') END;
--   json_value('{"o": {"b": "y"}}', '$.o') = None
CASE WHEN TYPEOF(GET_PATH(PARSE_JSON('{"o": {"b": "y"}}'), 'o')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT('{"o": {"b": "y"}}', 'o') END;
--   json_value('{"arr": ["z"]}', '$.arr') = None
CASE WHEN TYPEOF(GET_PATH(PARSE_JSON('{"arr": ["z"]}'), 'arr')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT('{"arr": ["z"]}', 'arr') END;
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
CASE WHEN TYPEOF(GET_PATH(PARSE_JSON('{"arr": ["z"]}'), 'arr[0]')) IN ('OBJECT', 'ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT('{"arr": ["z"]}', 'arr[0]') END;

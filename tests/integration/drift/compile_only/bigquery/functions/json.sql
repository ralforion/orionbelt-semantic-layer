-- bigquery · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
JSON_VALUE('{"a": "x"}', '$.a');
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
JSON_VALUE('{"o": {"b": "y"}}', '$.o.b');
--   json_value('{"n": 1}', '$.n') = '1'
JSON_VALUE('{"n": 1}', '$.n');
--   json_value('{"a": "x"}', '$.zz') = None
JSON_VALUE('{"a": "x"}', '$.zz');
--   json_value('{"o": {"b": "y"}}', '$.o') = None
JSON_VALUE('{"o": {"b": "y"}}', '$.o');
--   json_value('{"arr": ["z"]}', '$.arr') = None
JSON_VALUE('{"arr": ["z"]}', '$.arr');
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
JSON_VALUE('{"arr": ["z"]}', '$.arr[0]');
--   json_value('{"select": "x"}', '$.select') = 'x'
JSON_VALUE('{"select": "x"}', '$.select');

-- clickhouse · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
nullIf(JSON_VALUE('{"a": "x"}', '$.a'), '');
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
nullIf(JSON_VALUE('{"o": {"b": "y"}}', '$.o.b'), '');
--   json_value('{"n": 1}', '$.n') = '1'
nullIf(JSON_VALUE('{"n": 1}', '$.n'), '');
--   json_value('{"a": "x"}', '$.zz') = None
nullIf(JSON_VALUE('{"a": "x"}', '$.zz'), '');
--   json_value('{"o": {"b": "y"}}', '$.o') = None
nullIf(JSON_VALUE('{"o": {"b": "y"}}', '$.o'), '');
--   json_value('{"arr": ["z"]}', '$.arr') = None
nullIf(JSON_VALUE('{"arr": ["z"]}', '$.arr'), '');
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
nullIf(JSON_VALUE('{"arr": ["z"]}', '$.arr[0]'), '');

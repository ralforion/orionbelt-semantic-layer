-- databricks · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
try_variant_get(parse_json('{"a": "x"}'), '$.a', 'string');
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
try_variant_get(parse_json('{"o": {"b": "y"}}'), '$.o.b', 'string');
--   json_value('{"n": 1}', '$.n') = '1'
try_variant_get(parse_json('{"n": 1}'), '$.n', 'string');
--   json_value('{"a": "x"}', '$.zz') = None
try_variant_get(parse_json('{"a": "x"}'), '$.zz', 'string');
--   json_value('{"o": {"b": "y"}}', '$.o') = None
try_variant_get(parse_json('{"o": {"b": "y"}}'), '$.o', 'string');
--   json_value('{"arr": ["z"]}', '$.arr') = None
try_variant_get(parse_json('{"arr": ["z"]}'), '$.arr', 'string');
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
try_variant_get(parse_json('{"arr": ["z"]}'), '$.arr[0]', 'string');

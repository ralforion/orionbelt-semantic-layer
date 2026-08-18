-- databricks · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
CASE WHEN schema_of_variant(try_variant_get(parse_json('{"a": "x"}'), '$.a')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json('{"a": "x"}'), '$.a')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json('{"a": "x"}'), '$.a', 'string') END;
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
CASE WHEN schema_of_variant(try_variant_get(parse_json('{"o": {"b": "y"}}'), '$.o.b')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json('{"o": {"b": "y"}}'), '$.o.b')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json('{"o": {"b": "y"}}'), '$.o.b', 'string') END;
--   json_value('{"n": 1}', '$.n') = '1'
CASE WHEN schema_of_variant(try_variant_get(parse_json('{"n": 1}'), '$.n')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json('{"n": 1}'), '$.n')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json('{"n": 1}'), '$.n', 'string') END;
--   json_value('{"a": "x"}', '$.zz') = None
CASE WHEN schema_of_variant(try_variant_get(parse_json('{"a": "x"}'), '$.zz')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json('{"a": "x"}'), '$.zz')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json('{"a": "x"}'), '$.zz', 'string') END;
--   json_value('{"o": {"b": "y"}}', '$.o') = None
CASE WHEN schema_of_variant(try_variant_get(parse_json('{"o": {"b": "y"}}'), '$.o')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json('{"o": {"b": "y"}}'), '$.o')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json('{"o": {"b": "y"}}'), '$.o', 'string') END;
--   json_value('{"arr": ["z"]}', '$.arr') = None
CASE WHEN schema_of_variant(try_variant_get(parse_json('{"arr": ["z"]}'), '$.arr')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json('{"arr": ["z"]}'), '$.arr')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json('{"arr": ["z"]}'), '$.arr', 'string') END;
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
CASE WHEN schema_of_variant(try_variant_get(parse_json('{"arr": ["z"]}'), '$.arr[0]')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json('{"arr": ["z"]}'), '$.arr[0]')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json('{"arr": ["z"]}'), '$.arr[0]', 'string') END;
--   json_value('{"select": "x"}', '$.select') = 'x'
CASE WHEN schema_of_variant(try_variant_get(parse_json('{"select": "x"}'), '$.select')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json('{"select": "x"}'), '$.select')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json('{"select": "x"}'), '$.select', 'string') END;

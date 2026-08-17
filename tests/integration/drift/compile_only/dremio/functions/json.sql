-- dremio · json functions

-- json_value(x, path)
--   json_value('{"a": "x"}', '$.a') = 'x'
(TRY_CONVERT_FROM('{"a": "x"}' AS ROW(a VARCHAR)).a);
--   json_value('{"o": {"b": "y"}}', '$.o.b') = 'y'
(TRY_CONVERT_FROM('{"o": {"b": "y"}}' AS ROW(o ROW(b VARCHAR))).o.b);
--   json_value('{"n": 1}', '$.n') = '1'
(TRY_CONVERT_FROM('{"n": 1}' AS ROW(n VARCHAR)).n);
--   json_value('{"a": "x"}', '$.zz') = None
(TRY_CONVERT_FROM('{"a": "x"}' AS ROW(zz VARCHAR)).zz);
--   json_value('{"o": {"b": "y"}}', '$.o') = None
(TRY_CONVERT_FROM('{"o": {"b": "y"}}' AS ROW(o VARCHAR)).o);
--   json_value('{"arr": ["z"]}', '$.arr') = None
(TRY_CONVERT_FROM('{"arr": ["z"]}' AS ROW(arr VARCHAR)).arr);
--   json_value('{"arr": ["z"]}', '$.arr[0]') = 'z'
(TRY_CONVERT_FROM('{"arr": ["z"]}' AS ROW(arr LIST(VARCHAR))).arr[0]);

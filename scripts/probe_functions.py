"""Probe candidate scalar-function spellings against a live engine.

Runs one ``SELECT <expression>`` per candidate and prints what the engine did
with it: the value it returned, or the error it raised. The point is to learn
what each engine *accepts* and *returns* — a rename table can be derived from
syntax alone, but ``concat('a', NULL, 'c')`` and ``round(2.5)`` disagree on the
answer, not on the spelling, and only an executed probe shows that.

This is how a column of the portable-function catalog's support matrix gets
filled in for an engine we have not measured yet (``models/functions.py``).

Usage::

    set -a && source .env && set +a
    uv run python scripts/probe_functions.py duckdb      # local, no credentials
    uv run python scripts/probe_functions.py clickhouse  # CLICKHOUSE_*
    uv run python scripts/probe_functions.py postgres    # POSTGRES_*
    uv run python scripts/probe_functions.py mysql       # MYSQL_*
    uv run python scripts/probe_functions.py snowflake   # SNOWFLAKE_*
    uv run python scripts/probe_functions.py bigquery    # BIGQUERY_PROJECT + ADC
    uv run python scripts/probe_functions.py databricks  # DATABRICKS_* (warehouse running)
    uv run python scripts/probe_functions.py duckdb cast # one group only

The ``cast`` group is generated rather than listed: each case is rendered through
the engine's own ``cast_to_obml_type``, so the probe measures what OBSL emits
rather than a hand-spelled approximation of it.

Candidates are grouped so the output reads as a checklist: the canonical OBSL
form first, then the per-engine alternatives it may have to be rewritten into.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator

# (group, label, SQL expression). The expression is spliced into ``SELECT ...``
# verbatim, so it must be a scalar expression needing no FROM clause.
CANDIDATES: list[tuple[str, str, str]] = [
    ("jsonedge", "CH nullIf missing", """nullIf(JSON_VALUE('{"a": "x", "e": ""}', '$.zz'), '')"""),
    (
        "jsonedge",
        "CH nullIf empty-string value",
        """nullIf(JSON_VALUE('{"a": "x", "e": ""}', '$.e'), '')""",
    ),
    ("jsonedge", "CH value present", """nullIf(JSON_VALUE('{"a": "x", "e": ""}', '$.a'), '')"""),
    (
        "jsonedge",
        "PG cast + path_text",
        """json_extract_path_text('{"a": "x", "o": {"b": "y"}}'::json, 'a')""",
    ),
    (
        "jsonedge",
        "PG cast + nested",
        """json_extract_path_text('{"a": "x", "o": {"b": "y"}}'::json, 'o', 'b')""",
    ),
    (
        "jsonedge",
        "PG cast + missing",
        """json_extract_path_text('{"a": "x", "o": {"b": "y"}}'::json, 'zz')""",
    ),
    (
        "jsonedge",
        "SF nested dotted",
        """JSON_EXTRACT_PATH_TEXT('{"a": "x", "o": {"b": "y"}}', 'o.b')""",
    ),
    ("jsonedge", "SF missing", """JSON_EXTRACT_PATH_TEXT('{"a": "x", "o": {"b": "y"}}', 'zz')"""),
    ("jsonedge", "BQ missing", """JSON_VALUE('{"a": "x", "o": {"b": "y"}}', '$.zz')"""),
    ("jsonedge", "DDB missing", """json_extract_string('{"a": "x", "o": {"b": "y"}}', '$.zz')"""),
    # -- json: candidate spellings for the nested-access group ---------------
    # J is the same object everywhere so the answers are comparable:
    #   {"a": "x", "n": 1, "o": {"b": "y"}}
    ("json", "JSON_VALUE $.a", """JSON_VALUE('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.a')"""),
    ("json", "JSON_VALUE $.o.b", """JSON_VALUE('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.o.b')"""),
    (
        "json",
        "JSON_VALUE $.n (number)",
        """JSON_VALUE('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.n')""",
    ),
    (
        "json",
        "JSON_VALUE $.zz (missing)",
        """JSON_VALUE('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.zz')""",
    ),
    (
        "json",
        "json_extract_string",
        """json_extract_string('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.a')""",
    ),
    (
        "json",
        "json_extract_string nested",
        """json_extract_string('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.o.b')""",
    ),
    (
        "json",
        "json_extract_string number",
        """json_extract_string('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.n')""",
    ),
    (
        "json",
        "get_json_object",
        """get_json_object('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.a')""",
    ),
    (
        "json",
        "get_json_object number",
        """get_json_object('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.n')""",
    ),
    (
        "json",
        "JSON_UNQUOTE(JSON_EXTRACT)",
        """JSON_UNQUOTE(JSON_EXTRACT('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.a'))""",
    ),
    (
        "json",
        "JSONExtractString",
        """JSONExtractString('{"a": "x", "n": 1, "o": {"b": "y"}}', 'a')""",
    ),
    (
        "json",
        "JSONExtractString number",
        """JSONExtractString('{"a": "x", "n": 1, "o": {"b": "y"}}', 'n')""",
    ),
    (
        "json",
        "JSONExtractRaw number",
        """JSONExtractRaw('{"a": "x", "n": 1, "o": {"b": "y"}}', 'n')""",
    ),
    ("json", "jsonb #>> path", """('{"a": "x", "n": 1, "o": {"b": "y"}}'::jsonb #>> '{a}')"""),
    ("json", "jsonb #>> nested", """('{"a": "x", "n": 1, "o": {"b": "y"}}'::jsonb #>> '{o,b}')"""),
    ("json", "jsonb #>> number", """('{"a": "x", "n": 1, "o": {"b": "y"}}'::jsonb #>> '{n}')"""),
    ("json", "jsonb #>> missing", """('{"a": "x", "n": 1, "o": {"b": "y"}}'::jsonb #>> '{zz}')"""),
    (
        "json",
        "PARSE_JSON path text",
        """JSON_EXTRACT_PATH_TEXT(PARSE_JSON('{"a": "x", "n": 1, "o": {"b": "y"}}'), 'a')""",
    ),
    (
        "json",
        "PARSE_JSON path text nested",
        """JSON_EXTRACT_PATH_TEXT(PARSE_JSON('{"a": "x", "n": 1, "o": {"b": "y"}}'), 'o.b')""",
    ),
    (
        "json",
        "JSON_EXTRACT_PATH_TEXT raw str",
        """JSON_EXTRACT_PATH_TEXT('{"a": "x", "n": 1, "o": {"b": "y"}}', 'a')""",
    ),
    ("json", "JSON_QUERY subtree", """JSON_QUERY('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.o')"""),
    (
        "json",
        "json_extract subtree",
        """json_extract('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.o')""",
    ),
    ("json", "JSON_EXISTS", """JSON_EXISTS('{"a": "x", "n": 1, "o": {"b": "y"}}', '$.a')"""),
    # -- string: canonical forms and the rewrites they may need ---------------
    ("string", "substring(3-arg)", "substring('abcdef', 2, 3)"),
    ("string", "substring(2-arg)", "substring('abcdef', 2)"),
    ("string", "substr(3-arg)", "substr('abcdef', 2, 3)"),
    ("string", "substring(ANSI FROM)", "substring('abcdef' from 2 for 3)"),
    ("string", "concat", "concat('a', 'b', 'c')"),
    ("string", "concat NULL", "concat('a', NULL, 'c')"),
    ("string", "pipe concat", "'a' || 'b' || 'c'"),
    ("string", "pipe concat NULL", "'a' || NULL"),
    ("string", "upper/lower", "upper(lower('AbC'))"),
    ("string", "trim", "'[' || trim('  ab  ') || ']'"),
    ("string", "ltrim/rtrim", "'[' || ltrim(rtrim('  ab  ')) || ']'"),
    ("string", "length (unicode)", "length('äbcd')"),
    ("string", "char_length (unicode)", "char_length('äbcd')"),
    ("string", "lengthUTF8 (unicode)", "lengthUTF8('äbcd')"),
    ("string", "replace", "replace('abcab', 'ab', 'X')"),
    ("string", "position(x IN y)", "position('cd' in 'abcd')"),
    ("string", "position(x, y)", "position('cd', 'abcd')"),
    ("string", "strpos(y, x)", "strpos('abcd', 'cd')"),
    ("string", "instr(y, x)", "instr('abcd', 'cd')"),
    ("string", "locate(x, y)", "locate('cd', 'abcd')"),
    ("string", "split_part", "split_part('a,b,c', ',', 2)"),
    ("string", "split_part past end", "split_part('a,b,c', ',', 9)"),
    ("string", "splitByChar", "splitByChar(',', 'a,b,c')[2]"),
    ("string", "split offset", "split('a,b,c', ',')[safe_offset(1)]"),
    ("string", "substring_index", "substring_index(substring_index('a,b,c', ',', 2), ',', -1)"),
    ("string", "lpad/rpad", "lpad('7', 3, '0') || rpad('7', 3, '0')"),
    ("string", "leftPad/rightPad", "leftPad('7', 3, '0') || rightPad('7', 3, '0')"),
    ("string", "starts_with", "starts_with('abcd', 'ab')"),
    ("string", "startswith", "startswith('abcd', 'ab')"),
    ("string", "startsWith", "startsWith('abcd', 'ab')"),
    ("string", "ends_with", "ends_with('abcd', 'cd')"),
    ("string", "endswith", "endswith('abcd', 'cd')"),
    ("string", "endsWith", "endsWith('abcd', 'cd')"),
    ("string", "left(x, n) = prefix", "left('abcd', 2) = 'ab'"),
    ("string", "right(x, n) = suffix", "right('abcd', 2) = 'cd'"),
    # -- numeric --------------------------------------------------------------
    ("numeric", "abs", "abs(-3)"),
    ("numeric", "sign", "sign(-3)"),
    ("numeric", "floor/ceil", "floor(1.7) + ceil(1.2)"),
    ("numeric", "ceiling", "ceiling(1.2)"),
    ("numeric", "sqrt/ln/exp", "sqrt(4) + ln(1) + exp(0)"),
    ("numeric", "power", "power(2, 10)"),
    ("numeric", "pow", "pow(2, 10)"),
    ("numeric", "round 2dp", "round(2.345, 2)"),
    ("numeric", "round half 0.5", "round(0.5)"),
    ("numeric", "round half 2.5", "round(2.5)"),
    ("numeric", "round half -2.5", "round(-2.5)"),
    ("numeric", "round half-up rewrite", "sign(2.5) * floor(abs(2.5) + 0.5)"),
    ("numeric", "round half-up rewrite 2dp", "sign(2.345) * floor(abs(2.345) * 100 + 0.5) / 100"),
    ("numeric", "roundBankers", "roundBankers(2.5)"),
    ("numeric", "trunc", "trunc(1.9)"),
    ("numeric", "trunc negative", "trunc(-1.9)"),
    ("numeric", "trunc 2dp", "trunc(2.345, 2)"),
    ("numeric", "truncate 2dp", "truncate(2.345, 2)"),
    ("numeric", "mod", "mod(7, 3)"),
    ("numeric", "mod negative", "mod(-7, 3)"),
    ("numeric", "div(a, b)", "div(7, 2)"),
    ("numeric", "div negative", "div(-7, 2)"),
    ("numeric", "intDiv", "intDiv(-7, 2)"),
    ("numeric", "a DIV b", "-7 DIV 2"),
    ("numeric", "a // b", "-7 // 2"),
    ("numeric", "trunc(a / b)", "trunc(-7 / 2)"),
    ("numeric", "int division /", "7 / 2"),
    ("numeric", "log(base, x)", "log(10, 100)"),
    ("numeric", "log(x, base)", "log(100, 10)"),
    ("numeric", "log(x) one-arg", "log(100)"),
    ("numeric", "log10", "log10(100)"),
    ("numeric", "ln ratio rewrite", "ln(100) / ln(10)"),
    ("numeric", "greatest/least", "greatest(1, 2) + least(3, 4)"),
    # -- conditional ----------------------------------------------------------
    ("conditional", "coalesce", "coalesce(NULL, 'x')"),
    ("conditional", "coalesce all null", "coalesce(NULL, NULL)"),
    ("conditional", "nullif", "nullif('a', 'a')"),
    ("conditional", "nullif no match", "nullif('a', 'b')"),
    ("conditional", "greatest", "greatest(1, 2, 3)"),
    ("conditional", "greatest NULL", "greatest(1, NULL, 3)"),
    ("conditional", "least", "least(3, 2, 1)"),
    ("conditional", "least NULL", "least(3, NULL, 1)"),
    # -- date/time ------------------------------------------------------------
    # date_trunc: canonical is unit-first with a string unit.
    ("date", "date_trunc(unit, date)", "date_trunc('month', DATE '2026-08-15')"),
    ("date", "date_trunc(date, UNIT)", "date_trunc(DATE '2026-08-15', MONTH)"),
    ("date", "date_trunc quarter", "date_trunc('quarter', DATE '2026-08-15')"),
    ("date", "date_trunc week", "date_trunc('week', DATE '2026-08-15')"),
    ("date", "date_trunc hour", "date_trunc('hour', TIMESTAMP '2026-08-15 13:45:00')"),
    ("date", "toStartOfMonth", "toStartOfMonth(toDate('2026-08-15'))"),
    ("date", "DATE_FORMAT month", "DATE_FORMAT(DATE '2026-08-15', '%Y-%m-01')"),
    # date_add: canonical is unit-first; no engine accepts it, so every dialect
    # renders. The interval forms matter twice over, because n is an expression
    # in a real model, not a literal.
    ("date", "date_add(unit, n, date)", "date_add('day', 5, DATE '2026-08-01')"),
    ("date", "date_add(UNIT, n, date)", "date_add(DAY, 5, DATE '2026-08-01')"),
    ("date", "dateadd('unit', n, date)", "dateadd('day', 5, DATE '2026-08-01')"),
    ("date", "DATE_ADD(date, INTERVAL)", "DATE_ADD(DATE '2026-08-01', INTERVAL 5 DAY)"),
    ("date", "date + INTERVAL literal", "DATE '2026-08-01' + INTERVAL '5 day'"),
    ("date", "date + n * INTERVAL 1", "DATE '2026-08-01' + 5 * INTERVAL '1 day'"),
    ("date", "date + INTERVAL n DAY", "DATE '2026-08-01' + INTERVAL 5 DAY"),
    ("date", "TIMESTAMPADD", "TIMESTAMPADD(DAY, 5, DATE '2026-08-01')"),
    ("date", "date_add(date, days)", "date_add(DATE '2026-08-01', 5)"),
    ("date", "date_add negative", "date_add('day', -5, DATE '2026-08-01')"),
    ("date", "date_add month", "date_add('month', 1, DATE '2026-01-31')"),
    # date_diff: canonical is unit-first, end minus start, signed.
    ("date", "date_diff(unit, s, e)", "date_diff('day', DATE '2026-08-01', DATE '2026-08-15')"),
    ("date", "datediff(unit, s, e)", "datediff('day', DATE '2026-08-01', DATE '2026-08-15')"),
    ("date", "datediff(e, s)", "datediff(DATE '2026-08-15', DATE '2026-08-01')"),
    ("date", "DATE_DIFF(e, s, UNIT)", "DATE_DIFF(DATE '2026-08-15', DATE '2026-08-01', DAY)"),
    (
        "date",
        "TIMESTAMPDIFF(UNIT, s, e)",
        "TIMESTAMPDIFF(DAY, DATE '2026-08-01', DATE '2026-08-15')",
    ),
    ("date", "date subtraction", "DATE '2026-08-15' - DATE '2026-08-01'"),
    ("date", "date_diff month", "date_diff('month', DATE '2026-01-31', DATE '2026-03-01')"),
    (
        "date",
        "date_diff boundary vs whole",
        "date_diff('day', TIMESTAMP '2026-08-01 23:00:00', TIMESTAMP '2026-08-02 01:00:00')",
    ),
    ("date", "date_diff negative", "date_diff('day', DATE '2026-08-15', DATE '2026-08-01')"),
    # extract and friends
    ("date", "extract(unit FROM x)", "extract(year from DATE '2026-08-15')"),
    ("date", "extract quarter", "extract(quarter from DATE '2026-08-15')"),
    ("date", "extract week", "extract(week from DATE '2026-08-15')"),
    ("date", "extract dow", "extract(dow from DATE '2026-08-15')"),
    ("date", "extract dayofweek", "extract(dayofweek from DATE '2026-08-15')"),
    ("date", "date_part", "date_part('year', DATE '2026-08-15')"),
    ("date", "last_day", "last_day(DATE '2026-08-15')"),
    ("date", "current_date", "current_date"),
    ("date", "current_date()", "current_date()"),
    ("date", "current_timestamp", "current_timestamp"),
]


def _require_env(*names: str) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Missing environment variables: {', '.join(missing)}")


def run_duckdb() -> Iterator[Callable[[str], object]]:
    import duckdb

    con = duckdb.connect(":memory:")
    try:
        yield lambda sql: con.execute(sql).fetchone()[0]  # type: ignore[index]
    finally:
        con.close()


def run_clickhouse() -> Iterator[Callable[[str], object]]:
    import clickhouse_connect

    _require_env("CLICKHOUSE_HOST")
    client = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("CLICKHOUSE_USERNAME", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )
    try:
        yield lambda sql: client.query(sql).result_rows[0][0]
    finally:
        client.close()


def run_postgres() -> Iterator[Callable[[str], object]]:
    import psycopg2

    _require_env("POSTGRES_HOST", "POSTGRES_DBNAME", "POSTGRES_USER")
    conn = psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ["POSTGRES_DBNAME"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ.get("POSTGRES_PASSWORD", ""),
    )
    # Autocommit so one failing probe doesn't abort the rest of the session.
    conn.autocommit = True

    def _execute(sql: str) -> object:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]  # type: ignore[index]

    try:
        yield _execute
    finally:
        conn.close()


def run_mysql() -> Iterator[Callable[[str], object]]:
    import pymysql

    _require_env("MYSQL_HOST", "MYSQL_USER")
    conn = pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DATABASE"),
    )

    def _execute(sql: str) -> object:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]  # type: ignore[index]

    try:
        yield _execute
    finally:
        conn.close()


def run_snowflake() -> Iterator[Callable[[str], object]]:
    import snowflake.connector

    _require_env("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")
    con = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE"),
        schema=os.environ.get("SNOWFLAKE_SCHEMA"),
    )

    def _execute(sql: str) -> object:
        with con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]  # type: ignore[index]

    try:
        yield _execute
    finally:
        con.close()


def run_bigquery() -> Iterator[Callable[[str], object]]:
    from google.cloud import bigquery

    _require_env("BIGQUERY_PROJECT")
    client = bigquery.Client(
        project=os.environ["BIGQUERY_PROJECT"],
        location=os.environ.get("BIGQUERY_LOCATION"),
    )
    try:
        yield lambda sql: list(client.query(sql).result())[0][0]
    finally:
        client.close()


def run_databricks() -> Iterator[Callable[[str], object]]:
    from databricks import sql as dbsql

    _require_env("DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN")
    con = dbsql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER_HOSTNAME"],
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
        catalog=os.environ.get("DATABRICKS_CATALOG"),
    )

    def _execute(sql: str) -> object:
        with con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()[0]  # type: ignore[index]

    try:
        yield _execute
    finally:
        con.close()


ENGINES: dict[str, Callable[[], Iterator[Callable[[str], object]]]] = {
    "duckdb": run_duckdb,
    "clickhouse": run_clickhouse,
    "postgres": run_postgres,
    "mysql": run_mysql,
    "snowflake": run_snowflake,
    "bigquery": run_bigquery,
    "databricks": run_databricks,
}


#: A number, as each engine's regular-expression dialect spells one: an optional
#: sign, digits with an optional fractional part or a bare fraction, and an
#: optional exponent. The engines with no safe cast have to ask this *before*
#: converting, since MySQL's failure is a silent 0 rather than an error.
_NUMERIC_TEXT_RE = r"^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$"


def _pg_guard(value: str) -> str:
    return f"CASE WHEN {value} ~ '{_NUMERIC_TEXT_RE}' THEN CAST({value} AS NUMERIC) END"


def _my_guard(value: str) -> str:
    return f"CASE WHEN {value} REGEXP '{_NUMERIC_TEXT_RE}' THEN CAST({value} AS DOUBLE) END"


#: Candidate spellings for a conversion that answers NULL for text that is not a
#: number (#375). Listed per engine rather than generated: the whole question is
#: which safe form each engine *has*, and two of them have none.
TO_NUMBER_CANDIDATES: list[tuple[str, str, str]] = [
    ("to_number", "DDB try_cast '4.6'", "TRY_CAST('4.6' AS DOUBLE)"),
    ("to_number", "DDB try_cast 'abc'", "TRY_CAST('abc' AS DOUBLE)"),
    ("to_number", "DDB try_cast ''", "TRY_CAST('' AS DOUBLE)"),
    ("to_number", "DDB try_cast ' 42 '", "TRY_CAST(' 42 ' AS DOUBLE)"),
    ("to_number", "DDB try_cast '1e999'", "TRY_CAST('1e999' AS DOUBLE)"),
    ("to_number", "DDB try_cast number", "TRY_CAST(4.6 AS DOUBLE)"),
    ("to_number", "BQ safe_cast '4.6'", "SAFE_CAST('4.6' AS FLOAT64)"),
    ("to_number", "BQ safe_cast 'abc'", "SAFE_CAST('abc' AS FLOAT64)"),
    ("to_number", "BQ safe_cast '1e999'", "SAFE_CAST('1e999' AS FLOAT64)"),
    ("to_number", "SF try_cast '4.6'", "TRY_CAST('4.6' AS DOUBLE)"),
    ("to_number", "SF try_cast 'abc'", "TRY_CAST('abc' AS DOUBLE)"),
    ("to_number", "SF try_to_double 'abc'", "TRY_TO_DOUBLE('abc')"),
    ("to_number", "CH toFloat64OrNull '4.6'", "toFloat64OrNull('4.6')"),
    ("to_number", "CH toFloat64OrNull 'abc'", "toFloat64OrNull('abc')"),
    ("to_number", "CH toFloat64OrNull ' 42 '", "toFloat64OrNull(' 42 ')"),
    ("to_number", "CH toFloat64OrNull '1e999'", "toFloat64OrNull('1e999')"),
    ("to_number", "CH over toString(number)", "toFloat64OrNull(toString(4.6))"),
    ("to_number", "PG regex + numeric '4.6'", _pg_guard("'4.6'")),
    ("to_number", "PG regex + numeric 'abc'", _pg_guard("'abc'")),
    ("to_number", "PG regex + numeric ''", _pg_guard("''")),
    ("to_number", "PG regex + numeric ' 42 '", _pg_guard("' 42 '")),
    ("to_number", "PG regex + numeric '1e999'", _pg_guard("'1e999'")),
    ("to_number", "PG plain numeric '1e999'", "CAST('1e999' AS NUMERIC)"),
    (
        "to_number",
        "PG numeric -> double '1e999'",
        "CAST(CAST('1e999' AS NUMERIC) AS DOUBLE PRECISION)",
    ),
    ("to_number", "MY regexp + decimal '4.6'", _my_guard("'4.6'")),
    ("to_number", "MY regexp + decimal 'abc'", _my_guard("'abc'")),
    ("to_number", "MY regexp + decimal ''", _my_guard("''")),
    ("to_number", "MY regexp + decimal ' 42 '", _my_guard("' 42 '")),
    ("to_number", "MY regexp + decimal '1e999'", _my_guard("'1e999'")),
    ("to_number", "MY plain double '1e999'", "CAST('1e999' AS DOUBLE)"),
    ("to_number", "MY plain double 'abc'", "CAST('abc' AS DOUBLE)"),
]


#: Cast cases, as ``(label, input SQL literal, OBML type)``. Rendered per engine
#: through ``dialect.cast_to_obml_type`` rather than hand-spelled, so what is
#: measured is what OBSL would actually emit - including BigQuery's ROUND wrap
#: for a parameterized decimal and MySQL's widening (#336). The engines do not
#: merely disagree about spelling here: a float to integer rounds on four and
#: truncates on two, and ``'4.6'`` to integer returns 5, 4, an error or NULL
#: depending on where it runs (#355).
CAST_CASES: list[tuple[str, str, str]] = [
    # -- to decimal: does the fraction round or truncate, and which way at .5
    ("2.555 -> decimal(18,2)", "2.555", "decimal(18, 2)"),
    ("2.545 -> decimal(18,2)", "2.545", "decimal(18, 2)"),
    ("-2.555 -> decimal(18,2)", "-2.555", "decimal(18, 2)"),
    ("2.5 -> decimal(18,0)", "2.5", "decimal(18, 0)"),
    ("3.5 -> decimal(18,0)", "3.5", "decimal(18, 0)"),
    ("-2.5 -> decimal(18,0)", "-2.5", "decimal(18, 0)"),
    ("'4.6' -> decimal(18,2)", "'4.6'", "decimal(18, 2)"),
    ("'abc' -> decimal(18,2)", "'abc'", "decimal(18, 2)"),
    ("'' -> decimal(18,2)", "''", "decimal(18, 2)"),
    ("1e17+1 -> decimal(18,2)", "100000000000000001", "decimal(18, 2)"),
    # -- to double
    ("'4.6' -> double", "'4.6'", "double"),
    ("'abc' -> double", "'abc'", "double"),
    ("'' -> double", "''", "double"),
    ("1 -> double", "1", "double"),
    # -- to string: the trailing zero and the boolean split
    ("2.50 -> string", "2.50", "string"),
    ("1 -> string", "1", "string"),
    # -- to date
    ("'2026-08-15' -> date", "'2026-08-15'", "date"),
    ("'2026-8-5' -> date", "'2026-8-5'", "date"),
    ("'08/15/2026' -> date", "'08/15/2026'", "date"),
    ("'abc' -> date", "'abc'", "date"),
    # -- to timestamp
    ("'2026-08-15 13:45:00' -> ts", "'2026-08-15 13:45:00'", "timestamp"),
    ("'2026-08-15' -> ts", "'2026-08-15'", "timestamp"),
    ("'abc' -> ts", "'abc'", "timestamp"),
    # -- the targets v1 leaves out, measured so the reason stays checkable
    ("2.5 -> integer", "2.5", "integer"),
    ("3.5 -> integer", "3.5", "integer"),
    ("-2.5 -> integer", "-2.5", "integer"),
    ("'4.6' -> integer", "'4.6'", "integer"),
    ("'abc' -> integer", "'abc'", "integer"),
    ("'' -> integer", "''", "integer"),
]


def cast_candidates(engine: str) -> list[tuple[str, str, str]]:
    """:data:`CAST_CASES`, rendered the way *engine*'s dialect would emit them."""
    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get(engine)
    rendered: list[tuple[str, str, str]] = []
    for label, literal, obml_type in CAST_CASES:
        expr = dialect.cast_to_obml_type(RawSQL(sql=literal), parse_data_type(obml_type))
        rendered.append(("cast", label, dialect.compile_expr(expr)))
    return rendered


def probe(
    execute: Callable[[str], object],
    groups: set[str] | None = None,
    engine: str = "duckdb",
) -> None:
    """Run every candidate through *execute*, printing one line each."""
    current_group = ""
    for group, label, expression in [
        *CANDIDATES,
        *TO_NUMBER_CANDIDATES,
        *cast_candidates(engine),
    ]:
        if groups and group not in groups:
            continue
        if group != current_group:
            print(f"\n-- {group}")
            current_group = group
        try:
            value = execute(f"SELECT {expression}")
            print(f"  {label:24} OK    {value!r}")
        except Exception as exc:  # noqa: BLE001 — every failure mode is a result here
            detail = str(exc).splitlines()[0][:80] if str(exc).strip() else type(exc).__name__
            print(f"  {label:24} ERR   {detail}")


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ENGINES:
        print(f"usage: probe_functions.py <{' | '.join(ENGINES)}> [group ...]", file=sys.stderr)
        return 2
    engine = argv[1]
    groups = set(argv[2:]) or None
    print(f"===== {engine}")
    connect = ENGINES[engine]()
    execute = next(connect)
    try:
        probe(execute, groups, engine)
    finally:
        # Drain the generator so its ``finally`` closes the connection.
        for _ in connect:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

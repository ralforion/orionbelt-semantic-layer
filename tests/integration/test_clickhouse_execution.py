"""Integration tests: compile + execute the commerce battery on real ClickHouse.

The full ``COMMERCE_CASES`` battery defined in
``tests/integration/_commerce.py`` runs against a ClickHouse container.
DuckDB executes the same queries against the same parquet fixtures and acts
as the source of truth — any row-level disagreement is a ClickHouse dialect
bug.

Opt-in — requires Docker::

    uv run pytest -m docker

Skipped automatically when:
- testcontainers / clickhouse-connect / pandas / pyarrow are not installed
- the Docker daemon is not reachable
"""

from __future__ import annotations

import pytest

testcontainers_clickhouse = pytest.importorskip(
    "testcontainers.clickhouse", reason="testcontainers[clickhouse] required"
)
clickhouse_connect = pytest.importorskip("clickhouse_connect", reason="clickhouse-connect required")
pd = pytest.importorskip("pandas", reason="pandas required for bulk-load")
pytest.importorskip("pyarrow", reason="pyarrow required to read parquet")

from testcontainers.clickhouse import ClickHouseContainer  # noqa: E402

from tests.integration._commerce import (  # noqa: E402
    COMMERCE_CASES,
    COMMERCE_TABLES,
    CommerceCase,
    compare_rows,
    compile_for,
    fetch_duckdb,
    load_commerce_model,
    open_duckdb_truth,
    parquet_path,
)

pytestmark = pytest.mark.docker


# ClickHouse maps OBML ``schema`` → CH ``database``. We use a single named
# database matching the commerce model so compiled SQL (``orionbelt_1.sales``)
# resolves cleanly.
_SCHEMA = "orionbelt_1"


_CH_TYPE_MAP = {
    "int64": "Nullable(Int64)",
    "int32": "Nullable(Int32)",
    "float64": "Nullable(Float64)",
    "float32": "Nullable(Float32)",
    "bool": "Nullable(UInt8)",
}


def _docker_available() -> bool:
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


def _ch_type_for(dtype) -> str:
    s = str(dtype)
    if s.startswith("datetime64"):
        return "Nullable(DateTime)"
    if s == "object":
        return "Nullable(String)"
    return _CH_TYPE_MAP.get(s, "Nullable(String)")


def _load_parquet(client, schema: str, table: str) -> None:
    """CREATE TABLE + insert_df one parquet fixture via clickhouse-connect."""
    df = pd.read_parquet(parquet_path(table))
    # Convert date-only object columns (pyarrow surfaces those as object[date])
    # so CH can store them as Nullable(Date).
    for col in df.columns:
        if df[col].dtype == "object" and len(df) and hasattr(df[col].iloc[0], "isoformat"):
            df[col] = pd.to_datetime(df[col])

    cols_ddl = ", ".join(f"`{c}` {_ch_type_for(df[c].dtype)}" for c in df.columns)
    client.command(
        f"CREATE TABLE `{schema}`.`{table}` ({cols_ddl}) ENGINE = MergeTree() ORDER BY tuple()"
    )
    if df.empty:
        return
    client.insert_df(f"`{schema}`.`{table}`", df)


@pytest.fixture(scope="module")
def ch_setup():
    if not _docker_available():
        pytest.skip("Docker is not running")

    with ClickHouseContainer("clickhouse/clickhouse-server:latest") as ch:
        client = clickhouse_connect.get_client(
            host=ch.get_container_host_ip(),
            port=int(ch.get_exposed_port(8123)),
            username=ch.username,
            password=ch.password,
        )
        client.command(f"CREATE DATABASE `{_SCHEMA}`")
        for table in COMMERCE_TABLES:
            _load_parquet(client, _SCHEMA, table)
        yield client
        client.close()


@pytest.fixture(scope="module")
def vendor_model():
    return load_commerce_model(database="default", schema=_SCHEMA)


@pytest.fixture(scope="module")
def truth_model():
    return load_commerce_model(database="main", schema=_SCHEMA)


@pytest.fixture(scope="module")
def truth_results(truth_model):
    con = open_duckdb_truth(schema=_SCHEMA)
    try:
        return {
            case.name: fetch_duckdb(con, compile_for(case.query, truth_model, "duckdb"))
            for case in COMMERCE_CASES
        }
    finally:
        con.close()


def _fetch_clickhouse(client, sql: str) -> list[dict]:
    result = client.query(sql)
    return [dict(zip(result.column_names, row, strict=False)) for row in result.result_rows]


@pytest.mark.parametrize("case", COMMERCE_CASES, ids=lambda c: c.name)
def test_commerce_case(ch_setup, vendor_model, truth_results, case: CommerceCase) -> None:
    sql = compile_for(case.query, vendor_model, "clickhouse")
    actual = _fetch_clickhouse(ch_setup, sql)
    compare_rows(actual, truth_results[case.name], case=case.name)


def test_a_boolean_measure_survives_a_declared_decimal_type(ch_setup) -> None:
    """A boolean column aggregated under a declared decimal has to run.

    ClickHouse carries the type through MIN/MAX/any, and the decimal
    conversion reads its value as text, so ``MAX(flag)`` arrived as 'true' and
    raised CANNOT_PARSE_TEXT. The fix is at the layer that knows the column is
    boolean, so the engine is handed a number and the SQL needs no special
    case: through the dialect alone it could only be told apart from a string
    at run time, and every shape that did cost either the wrong answer for
    text or the operand written out three times.

    Executed through the compiler rather than a hand-written cast, because
    that is the only path carrying the column's declared type.

    On today's rendering this passes with or without the cast, because the
    engine is handed the flag and rounds it. It is here for the rendering that
    reads the value as text, where the flag arrives as 'true' and raises. The
    guard that bites either way is the emitted SQL, in
    ``tests/unit/test_boolean_measure_source.py``.
    """
    from decimal import Decimal
    from pathlib import Path
    from tempfile import mkdtemp

    from orionbelt.compiler.pipeline import CompilationPipeline
    from orionbelt.models.query import QueryObject, QuerySelect
    from orionbelt.parser.loader import TrackedLoader
    from orionbelt.parser.resolver import ReferenceResolver

    ch_setup.command("DROP TABLE IF EXISTS bool_measure")
    ch_setup.command("CREATE TABLE bool_measure (flag Bool, note String) ENGINE=Memory")
    ch_setup.command("INSERT INTO bool_measure VALUES (true, 'true'), (false, 'nope')")

    model_yaml = """version: 1.0
dataObjects:
  bool_measure:
    code: bool_measure
    columns:
      Flag: {code: flag, abstractType: boolean}
      Note: {code: note, abstractType: string}
measures:
  MaxFlag:
    columns: [{dataObject: bool_measure, column: Flag}]
    resultType: float
    dataType: "decimal(18, 2)"
    aggregation: max
  SumFlag:
    columns: [{dataObject: bool_measure, column: Flag}]
    resultType: float
    dataType: "decimal(18, 2)"
    aggregation: sum
  MaxNote:
    columns: [{dataObject: bool_measure, column: Note}]
    resultType: float
    dataType: "decimal(18, 2)"
    aggregation: max
  ExprFlag:
    expression: "{[bool_measure].[Flag]}"
    resultType: float
    dataType: "decimal(18, 2)"
    aggregation: max
"""
    path = Path(mkdtemp()) / "m.yaml"
    path.write_text(model_yaml)
    raw, source_map = TrackedLoader().load(path)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    pipeline = CompilationPipeline()

    def run(measure: str):
        sql = pipeline.compile(
            QueryObject(select=QuerySelect(measures=[measure])), model, "clickhouse"
        ).sql
        return ch_setup.query(sql).result_rows[0][0]

    assert run("MaxFlag") == Decimal("1.00")
    assert run("SumFlag") == Decimal("1.00")
    # The same measure written as an expression rather than a column list.
    # Two spellings of one thing, and the rule has to reach both.
    assert run("ExprFlag") == Decimal("1.00")

    # The string column is left alone. Reinterpreting the words a flag prints
    # would have turned this 'true' into 1.00 -- a number nobody wrote.
    with pytest.raises(Exception, match="(?i)cannot parse|exception"):
        run("MaxNote")

    ch_setup.command("DROP TABLE bool_measure")


@pytest.mark.parametrize(
    ("literal", "obml_type", "expected"),
    [
        # The measured defect: the Float64 nearest 2.55 sits just below it, so
        # rounding to 2 places and then truncating at 2 places returned 2.54.
        ("2.55", "decimal(18, 2)", "2.55"),
        ("toFloat64(2.55)", "decimal(18, 2)", "2.55"),
        # An exact Decimal source was never affected, and must not regress.
        ("toDecimal64('2.55', 2)", "decimal(18, 2)", "2.55"),
        # Ties still round away from zero, which is what the pre-round is for.
        ("2.545", "decimal(18, 2)", "2.55"),
        ("2.555", "decimal(18, 2)", "2.56"),
        ("-2.555", "decimal(18, 2)", "-2.56"),
        # Past Float64's exact-integer range the answer is the value the float
        # actually holds. Casting through a wide decimal instead returned
        # 12345678901234567.17, digits the input never had.
        ("12345678901234567.89", "decimal(19, 2)", "12345678901234568.00"),
        ("1e19", "decimal(38, 2)", "10000000000000000000.00"),
    ],
)
def test_decimal_cast_keeps_the_place_it_rounded_to(
    ch_setup, literal: str, obml_type: str, expected: str
) -> None:
    """A declared decimal type must not lose the place the pre-round decided."""
    from decimal import Decimal

    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    cast = dialect.cast_to_obml_type(RawSQL(sql=literal), parse_data_type(obml_type))
    rows = _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(cast)} AS v")
    assert rows[0]["v"] == Decimal(expected)


def test_decimal_cast_still_raises_on_input_it_cannot_read(ch_setup) -> None:
    """The implicit cast raises; only OBML's ``cast()`` answers NULL (#375).

    Converting through text is what makes the rounding exact, and it would
    equally make a date parse to NULL if it used the ``OrNull`` conversion the
    ``cast()`` path needs. It does not, so a type error stays a type error.
    """
    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    cast = dialect.cast_to_obml_type(
        RawSQL(sql="toDate('2026-08-15')"), parse_data_type("decimal(18, 2)")
    )
    with pytest.raises(Exception, match="(?i)cannot parse|illegal|exception"):
        _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(cast)} AS v")

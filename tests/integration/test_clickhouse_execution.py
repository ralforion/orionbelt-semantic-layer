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


def test_a_max_width_decimal_cast_reaches_its_target(ch_setup) -> None:
    """A value the target accepts must not fail on the way in.

    The exactness rewrite converts through an intermediate one place wider
    than the target, and Decimal256 is 76 digits wherever the point sits, so
    that place is taken from the integer side. For ``decimal(76, 20)`` the
    target holds 56 integer digits and the intermediate held 55, which meant a
    56-digit value ClickHouse would cast directly raised ARGUMENT_OUT_OF_BOUND
    through the rendering meant to make it exact.

    This is the CFL alignment width (``cfl_projection`` picks
    ``Decimal(76, 20)`` for ClickHouse), so it is reached by a multi-fact
    query rather than only by a hand-written cast.
    """
    from decimal import Decimal

    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    widest = "9" * 56
    cast = dialect.cast_to_obml_type(
        RawSQL(sql=f"toDecimal256('{widest}', 0)"), parse_data_type("decimal(76, 20)")
    )
    rows = _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(cast)} AS v")
    assert rows[0]["v"] == Decimal(widest)


def test_a_max_scale_decimal_cast_still_takes_an_integer_part(ch_setup) -> None:
    """``decimal(76, 75)`` holds one integer digit, and 1.5 has to fit in it.

    Clamping the intermediate on the scale alone left this asking for 76
    fractional places, which leaves no room for the 1.
    """
    from decimal import Decimal

    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    cast = dialect.cast_to_obml_type(RawSQL(sql="1.5"), parse_data_type("decimal(76, 75)"))
    rows = _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(cast)} AS v")
    assert rows[0]["v"] == Decimal("1.5")


@pytest.mark.parametrize(
    ("obml_type", "literal", "expected"),
    [
        # Below the ceiling the extra place exists and the tie rounds up.
        ("decimal(18, 2)", "2.555", "2.56"),
        ("decimal(75, 2)", "2.555", "2.56"),
        # At the ceiling it does not, and the conversion truncates. Recorded
        # rather than fixed: the place and the target's full integer width
        # cannot both exist in 76 digits. Declaring 75 restores the rounding.
        ("decimal(76, 2)", "2.555", "2.55"),
    ],
)
def test_rounding_at_and_below_the_precision_ceiling(
    ch_setup, obml_type: str, literal: str, expected: str
) -> None:
    from decimal import Decimal

    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    cast = dialect.cast_to_obml_type(RawSQL(sql=literal), parse_data_type(obml_type))
    rows = _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(cast)} AS v")
    assert rows[0]["v"] == Decimal(expected)


def test_a_decimal_request_above_the_ceiling_still_executes(ch_setup) -> None:
    """The final type is clamped to Decimal(76, s), so the intermediate must be.

    Through OBML's ``cast()`` rather than ``cast_to_obml_type``: only the
    catalog path carried the raw request, because the implicit path reads the
    scale back off the already-clamped rendered type. Computed from the raw
    request, ``decimal(77, 2)`` asked for an intermediate at scale 1 and lost a
    cent, and ``decimal(100, 2)`` asked for scale -22, which ClickHouse rejects
    before running.
    """
    from decimal import Decimal

    from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
    from orionbelt.dialect.registry import DialectRegistry

    dialect = DialectRegistry.get("clickhouse")
    for obml_type in ("decimal(77, 2)", "decimal(100, 2)"):
        ast = parse_expression(tokenize_metric_formula(f"cast('2.555', '{obml_type}')"))
        rows = _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(ast)} AS v")
        # Clamped to Decimal(76, 2), which is at the ceiling, so it truncates.
        assert rows[0]["v"] == Decimal("2.55"), obml_type


def test_a_value_wider_than_the_target_declares_still_lands(ch_setup) -> None:
    """This engine's CAST holds one digit more than the decimal declares.

    A 74-digit integer casts to ``Decimal(75, 2)``, which promises 73, and the
    conversion has to reach as far: an intermediate at scale 3 leaves 73 and
    answered ARGUMENT_OUT_OF_BOUND on a value the target itself took. The
    fallback converts at the target's own scale instead, so the value arrives
    truncated rather than not at all.
    """
    from decimal import Decimal

    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    widest = "1" + "0" * 73
    cast = dialect.cast_to_obml_type(
        RawSQL(sql=f"toDecimal256('{widest}', 0)"), parse_data_type("decimal(75, 2)")
    )
    rows = _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(cast)} AS v")
    assert rows[0]["v"] == Decimal(widest)


def test_the_same_value_through_cast_is_not_null(ch_setup) -> None:
    """``cast()`` answers NULL for input it cannot read, and this it can read.

    The ``OrNull`` conversion the contract needs made the width limit silent:
    the same 74-digit value came back as NULL rather than as an error, which is
    the shape of a value that does not name a number.
    """
    from decimal import Decimal

    from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
    from orionbelt.dialect.registry import DialectRegistry

    dialect = DialectRegistry.get("clickhouse")
    widest = "1" + "0" * 73
    ast = parse_expression(tokenize_metric_formula(f"cast('{widest}', 'decimal(75, 2)')"))
    rows = _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(ast)} AS v")
    assert rows[0]["v"] == Decimal(widest)


def test_a_wide_target_still_rounds_where_the_place_fits(ch_setup) -> None:
    """The fallback is a fallback: an ordinary value takes the rounded branch."""
    from decimal import Decimal

    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    cast = dialect.cast_to_obml_type(RawSQL(sql="2.555"), parse_data_type("decimal(75, 2)"))
    rows = _fetch_clickhouse(ch_setup, f"SELECT {dialect.compile_expr(cast)} AS v")
    assert rows[0]["v"] == Decimal("2.56")


@pytest.mark.parametrize(
    ("integer_digits", "expected_cents"),
    [(73, "56"), (74, "55")],
)
def test_where_the_seventy_seventh_digit_would_be_read_the_value_truncates(
    ch_setup, integer_digits: int, expected_cents: str
) -> None:
    """The documented residual, pinned on both sides of the boundary.

    Rounding reads one digit more than the value carries, and Decimal256 holds
    76. At 73 integer digits ``.555`` still has a place to be read from and
    rounds to ``.56``; at 74 the third decimal would be a seventy-seventh digit,
    so the conversion falls back to the target's own scale and truncates - even
    though ``.56`` would have fitted the target.

    Only text reaches this: no ClickHouse numeric type carries 77 significant
    digits, and a Float64 this large has no third decimal to round.
    """
    from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
    from orionbelt.dialect.registry import DialectRegistry

    dialect = DialectRegistry.get("clickhouse")
    whole = "1" + "0" * (integer_digits - 1)
    ast = parse_expression(tokenize_metric_formula(f"cast('{whole}.555', 'decimal(75, 2)')"))
    # Read back as text: the answer is what the engine computed, and a value
    # this wide does not survive the driver's own decimal context - 76
    # significant digits came back as 10000...000.6.
    rows = _fetch_clickhouse(ch_setup, f"SELECT toString({dialect.compile_expr(ast)}) AS v")
    assert rows[0]["v"] == f"{whole}.{expected_cents}"


def test_an_exact_operand_rounds_without_the_text_route(ch_setup) -> None:
    """The shortcut has to answer what the long way answers.

    A SUM over a column the model declares with a width cannot be a float, so
    the conversion through text has nothing to recover and the plain round is
    already exact. Both renderings are run here against the same rows, because
    the whole claim is that they agree.
    """
    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    ch_setup.command("CREATE TABLE IF NOT EXISTS exact_sum (amt Decimal(7, 2)) ENGINE = Memory")
    ch_setup.command("TRUNCATE TABLE exact_sum")
    ch_setup.command("INSERT INTO exact_sum VALUES (2.55), (0.005), (-1.10)")

    source = RawSQL(sql="SUM(amt)")
    target = parse_data_type("decimal(18, 2)")
    shortcut = dialect.compile_expr(dialect.cast_to_obml_type(source, target, source_exact=True))
    long_way = dialect.compile_expr(dialect.cast_to_obml_type(source, target))
    assert "toString(" not in shortcut, shortcut
    assert "toString(" in long_way, long_way

    rows = _fetch_clickhouse(ch_setup, f"SELECT {shortcut} AS a, {long_way} AS b FROM exact_sum")
    assert rows[0]["a"] == rows[0]["b"], rows[0]
    ch_setup.command("DROP TABLE exact_sum")


@pytest.mark.parametrize("session_zone", ["Europe/Berlin", "America/New_York", "UTC"])
def test_a_declared_timestamp_is_the_same_wall_clock_on_any_server(
    ch_setup, session_zone: str
) -> None:
    """The server's timezone must not decide what a declared timestamp says.

    ClickHouse's DateTime is an instant that renders against the server's own
    zone, so before this the same stored value answered 13:45 on a Berlin
    deployment and 07:45 on a New York one. The conversion reads the wall clock
    as text and labels it UTC, which is a label the reading has made true.

    ``session_timezone`` stands in for the server setting, so the case is
    reachable from a container that runs UTC.
    """
    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    stored = RawSQL(sql="CAST('2026-08-15 13:45:00' AS DateTime64(3))")
    sql = dialect.compile_expr(dialect.cast_to_obml_type(stored, parse_data_type("timestamp")))
    # Read back in UTC, which is what makes the assertion say anything: a
    # plain toString renders in the session zone, so the old rendering and this
    # one would print the same text for different instants.
    rows = _fetch_clickhouse(
        ch_setup,
        f"SELECT toString(toTimeZone({sql}, 'UTC')) AS v "
        f"SETTINGS session_timezone = '{session_zone}'",
    )
    assert rows[0]["v"] == "2026-08-15 13:45:00.000"


def test_the_wall_clock_survives_a_dst_boundary(ch_setup) -> None:
    """The offset differs by season, so nothing here may apply one."""
    from orionbelt.ast.nodes import RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get("clickhouse")
    for stamp in ("2026-08-15 13:45:00", "2026-01-15 13:45:00"):
        stored = RawSQL(sql=f"CAST('{stamp}' AS DateTime64(3))")
        sql = dialect.compile_expr(dialect.cast_to_obml_type(stored, parse_data_type("timestamp")))
        rows = _fetch_clickhouse(
            ch_setup,
            f"SELECT toString(toTimeZone({sql}, 'UTC')) AS v "
            f"SETTINGS session_timezone = 'Europe/Berlin'",
        )
        assert rows[0]["v"] == f"{stamp}.000", stamp

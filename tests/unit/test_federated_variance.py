"""A federated ``variance`` or ``stddev``: from the legs' exact moments.

Each group's rows sit under several federation keys, so the fact leg returns
a count, a sum and a sum of squares per key and the combine step computes the
statistic from their totals. The rows are the cases a double gets wrong:
values far from zero, where ``S2 - S1^2 / N`` cancels in every digit a double
holds; squares past 64 bits; a constant; one value; no values. The reference
is exact arithmetic (fractions).
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")

from orionbelt.compiler.federation import FederatedLeg  # noqa: E402
from orionbelt.dialect.registry import DialectRegistry  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.service.db_executor import ExecutionError  # noqa: E402
from orionbelt.service.federated_executor import (  # noqa: E402
    FederatedLegError,
    combine,
    normalize_leg,
)
from orionbelt.service.model_store import ModelStore  # noqa: E402

# label -> (i, d) per row
VALUES: dict[str, list[tuple[int | None, str | None]]] = {
    "small": [(1, "1.10"), (2, "2.25"), (2, "2.25"), (3, "3.40"), (7, "7.05"), (4, "4.00")],
    "far": [(10**9 + k, f"{10_000_000 + k / 100:.2f}") for k in range(10)],
    "big": [(10**15 + k, str(Decimal("9999999999999999.99") - Decimal(k) / 100)) for k in range(6)],
    "huge": [(4 * 10**18 + k, "1.00") for k in range(3)],
    "constant": [(123456789, "1234567.89")] * 4,
    "one": [(42, "42.42")],
    "nulls": [(None, None), (10, "10.00"), (20, "20.00")],
    "empty": [(None, None), (None, None)],
    "negative": [(-5, "-5.50"), (5, "5.50"), (-3, "-3.25"), (3, "3.25")],
}

# (label, key, i, d): each label's rows under up to three keys.
ROWS = [
    (label, 10 * n + r % 3, i, d)
    for n, (label, rows) in enumerate(VALUES.items())
    for r, (i, d) in enumerate(rows)
]

MODEL = """\
version: 1.0
settings:
  defaultDialect: duckdb
dataObjects:
  Facts:
    code: facts
    dataSource: facts
    columns:
      Key: {code: k, abstractType: int}
      I: {code: i, abstractType: int}
      D: {code: d, abstractType: decimal, sqlPrecision: 18, sqlScale: 2}
      Bare D: {code: d, abstractType: decimal}
      F: {code: f, abstractType: float}
    joins:
      - {joinType: many-to-one, joinTo: Groups, columnsFrom: [Key], columnsTo: [Group Key]}
  Groups:
    code: groups
    dataSource: labels
    columns:
      Group Key: {code: k, abstractType: int}
      Label: {code: g, abstractType: string}
dimensions:
  Label: {dataObject: Groups, column: Label, resultType: string}
measures:
  Var I: {columns: [{dataObject: Facts, column: I}], aggregation: variance, resultType: float}
  Var Pop I: {columns: [{dataObject: Facts, column: I}], aggregation: var_pop, resultType: float}
  SD I: {columns: [{dataObject: Facts, column: I}], aggregation: stddev, resultType: float}
  SD Pop I: {columns: [{dataObject: Facts, column: I}], aggregation: stddev_pop, resultType: float}
  Var D: {columns: [{dataObject: Facts, column: D}], aggregation: variance, resultType: float}
  SD Pop D: {columns: [{dataObject: Facts, column: D}], aggregation: stddev_pop, resultType: float}
  Var Twice I:
    expression: "{[Facts].[I]} * 2"
    aggregation: variance
    resultType: float
  Var Positive I:
    columns: [{dataObject: Facts, column: I}]
    aggregation: variance
    resultType: float
    filters:
      - column: {dataObject: Facts, column: I}
        operator: gt
        values: [{dataType: int, valueInt: 0}]
  Var F: {columns: [{dataObject: Facts, column: F}], aggregation: variance, resultType: float}
  Var Bare D:
    columns: [{dataObject: Facts, column: Bare D}]
    aggregation: variance
    resultType: float
  Var Total I:
    columns: [{dataObject: Facts, column: I}]
    aggregation: variance
    resultType: float
    total: true
  Corr I D:
    columns: [{dataObject: Facts, column: I}, {dataObject: Facts, column: D}]
    aggregation: corr
    resultType: float
  __var0_squares: {columns: [{dataObject: Facts, column: I}], aggregation: count, resultType: int}
metrics:
  SD I Doubled: {expression: "{[SD I]} * 2"}
"""


@pytest.fixture(scope="module")
def files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("variance")
    paths = {name: str(root / f"{name}.duckdb") for name in ("facts", "labels", "all")}
    for name in ("facts", "all"):
        con = duckdb.connect(paths[name])
        con.execute("CREATE TABLE facts (k INTEGER, i BIGINT, d DECIMAL(18, 2), f DOUBLE)")
        con.executemany(
            "INSERT INTO facts VALUES (?, ?, CAST(? AS DECIMAL(18, 2)), ?)",
            [(k, i, d, None if i is None else float(i)) for _, k, i, d in ROWS],
        )
        con.close()
    for name in ("labels", "all"):
        con = duckdb.connect(paths[name])
        con.execute("CREATE TABLE groups (k INTEGER, g VARCHAR)")
        con.executemany("INSERT INTO groups VALUES (?, ?)", sorted({(k, g) for g, k, *_ in ROWS}))
        con.close()
    return paths


def compile_query(query: dict[str, Any], dialect: str = "duckdb") -> Any:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    return store.compile_query(model_id, QueryObject.model_validate(query), dialect)


def run(
    files: dict[str, str], measures: list[str], where: list[dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    query = {"select": {"dimensions": ["Label"], "measures": measures}, "where": where or []}
    plan = compile_query(query).federation
    assert plan is not None
    tables = {}
    for leg in plan.legs:
        con = duckdb.connect(files[leg.data_source], read_only=True)
        try:
            tables[leg.name] = normalize_leg(leg, con.sql(leg.sql).to_arrow_table())
        finally:
            con.close()
    rows = combine(plan, tables, None).arrow_table.to_pylist()
    return {r["Label"]: r for r in rows}


def run_single(files: dict[str, str], measures: list[str]) -> dict[str, dict[str, Any]]:
    """The same query on one source holding both tables."""
    single = MODEL.replace("    dataSource: facts\n", "").replace("    dataSource: labels\n", "")
    store = ModelStore()
    model_id = store.load_model(single).model_id
    query = QueryObject.model_validate({"select": {"dimensions": ["Label"], "measures": measures}})
    compiled = store.compile_query(model_id, query, "duckdb")
    assert compiled.federation is None
    con = duckdb.connect(files["all"], read_only=True)
    try:
        rows = con.sql(compiled.sql).to_arrow_table().to_pylist()
    finally:
        con.close()
    return {r["Label"]: r for r in rows}


def variance(values: list[Any], *, sample: bool) -> Fraction | None:
    present = [Fraction(Decimal(str(v))) for v in values if v is not None]
    if len(present) < (2 if sample else 1):
        return None
    mean = sum(present) / len(present)
    spread = sum((v - mean) ** 2 for v in present)
    return spread / (len(present) - 1 if sample else len(present))


def assert_statistic(got: float | None, want: Fraction | None, *, root: bool) -> None:
    """*got* is the double nearest *want* (or its square root), give or take
    the few roundings after the exact part."""
    if want is None:
        assert got is None
        return
    assert got is not None
    value = Fraction(got) ** 2 if root else Fraction(got)
    if want == 0:
        assert value == 0
    else:
        assert abs((value - want) / want) < Fraction(1, 10**14)


CASES = {
    "Var I": (0, True, False),
    "Var Pop I": (0, False, False),
    "SD I": (0, True, True),
    "SD Pop I": (0, False, True),
    "Var D": (1, True, False),
    "SD Pop D": (1, False, True),
}


class TestExactMoments:
    @pytest.mark.parametrize("label", list(VALUES))
    @pytest.mark.parametrize("measure", list(CASES))
    def test_the_statistic_is_the_exact_one_to_a_doubles_precision(
        self, files: dict[str, str], measure: str, label: str
    ) -> None:
        """Far from zero a double's ``S2 - S1^2 / N`` cancels to noise (the
        variance of 4e18 + 0, 1, 2 is 1); integer moments do not."""
        column, sample, root = CASES[measure]
        got = run(files, [measure])[label][measure]

        values = [row[column] for row in VALUES[label]]
        assert_statistic(got, variance(values, sample=sample), root=root)

    def test_an_expression_of_integers_is_squared_as_one(self, files: dict[str, str]) -> None:
        got = run(files, ["Var Twice I"], [{"field": "Label", "op": "!=", "value": "huge"}])

        for label, rows in VALUES.items():
            if label == "huge":
                continue
            values = [None if i is None else 2 * i for i, _ in rows]
            assert_statistic(got[label]["Var Twice I"], variance(values, sample=True), root=False)

    def test_squares_past_128_bits_raise_rather_than_wrap(self, files: dict[str, str]) -> None:
        """8e18 squared three times sums past 2^127, which the combine step's
        integers hold no more: DuckDB raises, as one engine would on its own
        overflow."""
        with pytest.raises(ExecutionError, match="Overflow"):
            run(files, ["Var Twice I"], [{"field": "Label", "op": "=", "value": "huge"}])

    def test_a_measure_filter_applies_to_every_moment(self, files: dict[str, str]) -> None:
        got = run(files, ["Var Positive I"])["negative"]["Var Positive I"]

        assert_statistic(got, variance([5, 3], sample=True), root=False)

    def test_a_metric_reads_the_combined_statistic(self, files: dict[str, str]) -> None:
        got = run(files, ["SD I Doubled"])["small"]["SD I Doubled"]

        # At the metric's default type, decimal(18, 2), as on one source:
        # twice the square root of 82/15.
        assert got == Decimal("4.27")

    def test_a_declared_measure_named_like_a_partial_keeps_both(
        self, files: dict[str, str]
    ) -> None:
        got = run(files, ["Var I", "__var0_squares"])["nulls"]

        assert got["__var0_squares"] == 2
        assert_statistic(got["Var I"], Fraction(50), root=False)


class TestPlan:
    def test_the_leg_returns_moments_with_each_value_squared(self) -> None:
        plan = compile_query({"select": {"dimensions": ["Label"], "measures": ["Var D"]}})
        fact = plan.federation.legs[0]

        assert "VARIANCE" not in fact.sql.upper()
        assert 'SUM(CAST("Facts"."d" AS DECIMAL(38, 2)) * CAST("Facts"."d" AS DECIMAL(38, 2)))' in (
            fact.sql
        )
        # A declared measure takes __var0_squares, so the leg's column steps aside.
        assert fact.integer_sums == (("__var0_sum", 2), ("___var0_squares", 4))

    def test_an_integer_statistic_reads_its_moments_at_scale_zero(self) -> None:
        plan = compile_query({"select": {"dimensions": ["Label"], "measures": ["SD I"]}})

        assert [scale for _, scale in plan.federation.legs[0].integer_sums] == [0, 0]

    @pytest.mark.parametrize("dialect", ["databricks", "dremio"])
    def test_four_places_square_exactly_on_duckdb_but_come_from_rows_on(self, dialect: str) -> None:
        wider = MODEL.replace("sqlPrecision: 18, sqlScale: 2", "sqlPrecision: 18, sqlScale: 4")
        store = ModelStore()
        model_id = store.load_model(wider).model_id
        query = QueryObject.model_validate(
            {"select": {"dimensions": ["Label"], "measures": ["Var D"]}}
        )

        squared = store.compile_query(model_id, query, "duckdb").federation.legs[0]
        assert squared.integer_sums and squared.rows is None
        from_rows = store.compile_query(model_id, query, dialect).federation.legs[0]
        assert not from_rows.integer_sums and from_rows.rows is not None


FROM_ROWS = {
    "a float column": "Var F",
    "a decimal without a declared width": "Var Bare D",
    "total: true": "Var Total I",
    "a correlation": "Corr I D",
}


# Far from zero a statistic of doubles depends on the order its values are
# added in, on one source too: DuckDB's own VAR_SAMP of the doubles 1e15 + 0..5
# is 3.5 in that order, 3.4875 in the order the fact leg returns them (grouped
# by key) and 3.525 in another, measured.
NEAR_ZERO = [label for label in VALUES if label not in ("far", "big", "huge")]


class TestFromRows:
    """What its moments do not combine exactly is computed from the fact leg's rows."""

    @pytest.mark.parametrize("measure", FROM_ROWS.values(), ids=FROM_ROWS.keys())
    def test_it_is_what_one_source_returns(self, files: dict[str, str], measure: str) -> None:
        federated = run(files, [measure])
        single = run_single(files, [measure])

        assert federated.keys() == single.keys()
        for label in NEAR_ZERO:
            assert federated[label][measure] == single[label][measure], label


SQUARES = {
    "duckdb": 'CAST("x" AS DECIMAL(38, 2)) * CAST("x" AS DECIMAL(38, 2))',
    "postgres": 'CAST("x" AS NUMERIC) * CAST("x" AS NUMERIC)',
    "mysql": "CAST(`x` AS DECIMAL(27, 2)) * CAST(`x` AS DECIMAL(27, 2))",
    "snowflake": 'CAST("x" AS NUMBER(38, 2)) * CAST("x" AS NUMBER(38, 2))',
    "bigquery": "CAST(`x` AS BIGNUMERIC) * CAST(`x` AS BIGNUMERIC)",
    "clickhouse": 'toDecimal256("x", 2) * toDecimal256("x", 2)',
    "dremio": 'CAST("x" AS DECIMAL(38, 2)) * CAST("x" AS DECIMAL(38, 2))',
    "databricks": "CAST(`x` AS DECIMAL(38, 2)) * CAST(`x` AS DECIMAL(38, 2))",
}

# The most places each engine squares exactly, measured or by its rule.
MOST_PLACES = {
    "duckdb": 19,
    "postgres": 16383,
    "mysql": 15,
    "snowflake": 6,
    "bigquery": 19,
    "clickhouse": 38,
    "dremio": 3,
    "databricks": 3,
}


class TestExactSquare:
    @pytest.mark.parametrize("dialect", list(SQUARES))
    def test_each_engine_squares_in_a_type_wide_enough(self, dialect: str) -> None:
        from orionbelt.ast.nodes import ColumnRef

        engine = DialectRegistry.get(dialect)
        square = engine.exact_square(ColumnRef(name="x"), 2)

        assert square is not None
        assert engine.compile_expr(square) == SQUARES[dialect]

    @pytest.mark.parametrize("dialect", list(MOST_PLACES))
    def test_past_its_places_an_engine_squares_nothing(self, dialect: str) -> None:
        from orionbelt.ast.nodes import ColumnRef

        engine = DialectRegistry.get(dialect)
        most = MOST_PLACES[dialect]

        assert engine.exact_square(ColumnRef(name="x"), most) is not None
        assert engine.exact_square(ColumnRef(name="x"), most + 1) is None


def leg(*sums: tuple[str, int]) -> FederatedLeg:
    return FederatedLeg(
        name="leg_0",
        data_source="facts",
        dialect="bigquery",
        sql="SELECT ...",
        data_objects=("Facts",),
        columns=tuple((name, "decimal") for name, _ in sums),
        integer_sums=sums,
    )


class TestWholeUnits:
    """A variance's sums are read as whole numbers of their scale's units."""

    def test_a_decimal_sum_becomes_its_units(self) -> None:
        table = pa.table({"s": pa.array([Decimal("1.25"), None], pa.decimal128(38, 2))})

        out = normalize_leg(leg(("s", 4)), table)

        assert out.schema.field("s").type == pa.decimal128(38, 0)
        assert out.column("s").to_pylist() == [12500, None]

    def test_bigquerys_38_places_narrow_once_rescaled(self) -> None:
        """BIGNUMERIC arrives as ``decimal256(76, 38)``: narrowed at its own
        scale it would not fit, and was refused."""
        value = Decimal("99999999999999999999999999999999.9999")
        table = pa.table({"s": pa.array([value], pa.decimal256(76, 38))})

        out = normalize_leg(leg(("s", 4)), table)

        assert out.column("s").to_pylist() == [999999999999999999999999999999999999]

    def test_postgres_numeric_text_becomes_units(self) -> None:
        table = pa.table({"s": pa.array(["-12.5", "0.0001"])})

        out = normalize_leg(leg(("s", 4)), table)

        assert out.column("s").to_pylist() == [-125000, 1]

    def test_a_sum_with_more_places_is_refused_not_rounded(self) -> None:
        table = pa.table({"s": pa.array([Decimal("1.00001")], pa.decimal128(38, 5))})

        with pytest.raises(FederatedLegError, match="sums of a variance .* more than 4"):
            normalize_leg(leg(("s", 4)), table)

    def test_a_floating_point_sum_is_refused(self) -> None:
        table = pa.table({"s": pa.array([1.5], pa.float64())})

        with pytest.raises(FederatedLegError, match="sums of a variance .* floating point"):
            normalize_leg(leg(("s", 0)), table)

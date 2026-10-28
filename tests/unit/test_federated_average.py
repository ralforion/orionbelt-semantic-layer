"""A federated ``avg``: the legs' partial sums over their counts, divided exactly.

The rows are the hard cases of the probe that decided the route (integers past
2**53, a tie past a double's mantissa, negatives, an empty group, a decimal at
its declared width). Each group's rows sit under two federation keys, so the
combine step adds two partial results per group, and the answer is checked
against exact decimal arithmetic rounded half away from zero, the way the
exact engines round it. DuckDB's own ``AVG`` is a double, and fails to cast
the widest group back to ``DECIMAL(18, 2)``, so it is not the reference here.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")
pa = pytest.importorskip("pyarrow")

from orionbelt.compiler.federation import FederatedLeg  # noqa: E402
from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.service.federated_executor import (  # noqa: E402
    FederatedLegError,
    combine,
    normalize_leg,
)
from orionbelt.service.model_store import ModelStore  # noqa: E402

# (group, key, i, d, f)
ROWS = [
    ("a", 1, 1000000000000000002, "12345678901234.56", 0.1),
    ("a", 2, 1000000000000000004, "12345678901234.57", 0.2),
    ("b", 3, 1, "1.00", 1.0),
    ("b", 4, 2, "2.00", 1.0),
    ("b", 4, 2, "2.00", 2.0),
    ("c", 5, -7, "-2.36", -0.5),
    ("c", 6, -8, "-2.37", -0.25),
    ("e", 7, None, None, None),
    ("e", 8, None, None, None),
    ("h", 9, 9000000000000000000, "9999999999999999.99", 1e300),
    ("h", 10, 9000000000000000000, "9999999999999999.99", 1e300),
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
      D: {code: d, abstractType: decimal, numClass: additive}
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
  Avg I: {columns: [{dataObject: Facts, column: I}], aggregation: avg, resultType: int}
  Avg D: {columns: [{dataObject: Facts, column: D}], aggregation: avg, resultType: decimal}
  Avg F: {columns: [{dataObject: Facts, column: F}], aggregation: avg, resultType: float}
  Avg D Wide:
    columns: [{dataObject: Facts, column: D}]
    aggregation: avg
    dataType: "decimal(38, 18)"
  __avg0_sum: {columns: [{dataObject: Facts, column: D}], aggregation: count, resultType: int}
metrics:
  Avg D Thousand: {expression: "{[Avg D]} * 1000"}
"""


@pytest.fixture(scope="module")
def files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("avg")
    paths = {"facts": str(root / "facts.duckdb"), "labels": str(root / "labels.duckdb")}
    con = duckdb.connect(paths["facts"])
    con.execute("CREATE TABLE facts (k INTEGER, i BIGINT, d DECIMAL(18, 2), f DOUBLE)")
    con.executemany(
        "INSERT INTO facts VALUES (?, ?, CAST(? AS DECIMAL(18, 2)), ?)",
        [(k, i, d, f) for _, k, i, d, f in ROWS],
    )
    con.close()
    con = duckdb.connect(paths["labels"])
    con.execute("CREATE TABLE groups (k INTEGER, g VARCHAR)")
    con.executemany("INSERT INTO groups VALUES (?, ?)", sorted({(k, g) for g, k, *_ in ROWS}))
    con.close()
    return paths


def run(
    files: dict[str, str], measures: list[str], where: list[dict[str, Any]] | None = None
) -> dict[str, dict[str, Any]]:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    query = QueryObject.model_validate(
        {"select": {"dimensions": ["Label"], "measures": measures}, "where": where or []}
    )
    compiled = store.compile_query(model_id, query, "duckdb")
    plan = compiled.federation
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


def exact(values: list[Any], places: int = 2) -> Decimal | None:
    present = [Decimal(str(v)) for v in values if v is not None]
    if not present:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        return (sum(present) / len(present)).quantize(
            Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP
        )


GROUPS = sorted({g for g, *_ in ROWS})


class TestExactAverage:
    @pytest.mark.parametrize("group", GROUPS)
    def test_an_integer_average_is_exact(self, files: dict[str, str], group: str) -> None:
        got = run(files, ["Avg I"])[group]["Avg I"]

        assert got == exact([i for g, _, i, _, _ in ROWS if g == group])

    @pytest.mark.parametrize("group", GROUPS)
    def test_a_decimal_average_is_exact(self, files: dict[str, str], group: str) -> None:
        """Past a double's mantissa: 12345678901234.565 rounds to .57, which a
        double division gives as .56; and 9999999999999999.99 survives where a
        double becomes 1E+16 and overflows the measure's type."""
        got = run(files, ["Avg D"])[group]["Avg D"]

        assert got == exact([d for g, _, _, d, _ in ROWS if g == group])

    @pytest.mark.parametrize("group", GROUPS)
    def test_a_wide_scale_does_not_overflow(self, files: dict[str, str], group: str) -> None:
        """``decimal(38, 18)`` over values near 10^16: scaling the whole total
        by 10^18 before dividing overflowed 128 bits; dividing the integer part
        first keeps every intermediate below the count times 10^18."""
        got = run(files, ["Avg D Wide"])[group]["Avg D Wide"]

        assert got == exact([d for g, _, _, d, _ in ROWS if g == group], places=18)

    def test_a_metric_reads_the_average_unrounded(self, files: dict[str, str]) -> None:
        """1, 2 and 2 average to 1.666..., so the metric is 1666.67; reading
        the average rounded to the measure's two places gave 1670.00."""
        # Group b alone: a's average times 1000 does not fit the metric's
        # decimal(18, 2), on one source as here.
        got = run(files, ["Avg D Thousand"], [{"field": "Label", "op": "=", "value": "b"}])
        got = got["b"]["Avg D Thousand"]

        assert got == pytest.approx(Decimal("1666.67"), abs=Decimal("0.01"))

    def test_a_declared_measure_named_like_a_partial_keeps_both(
        self, files: dict[str, str]
    ) -> None:
        got = run(files, ["Avg D", "__avg0_sum"])["b"]

        # A count, not the sum the average divides: in its place, the average
        # came out as the count over the count.
        assert got["Avg D"] == Decimal("1.67")
        assert got["__avg0_sum"] == 3

    def test_a_float_average_divides_its_doubles(self, files: dict[str, str]) -> None:
        got = run(files, ["Avg F"])

        assert got["b"]["Avg F"] == pytest.approx(4.0 / 3)
        assert got["c"]["Avg F"] == pytest.approx(-0.375)
        assert got["e"]["Avg F"] is None


def leg(*sums: tuple[str, int]) -> FederatedLeg:
    return FederatedLeg(
        name="leg_0",
        data_source="facts",
        dialect="postgres",
        sql="SELECT ...",
        data_objects=("Facts",),
        columns=tuple((name, "decimal") for name, _ in sums),
        exact_sums=sums,
    )


class TestPartialSums:
    """The sums an average divides are read at its scale, exactly or not at all."""

    def test_a_sum_at_fewer_places_widens_to_the_scale(self) -> None:
        table = pa.table({"s": pa.array([Decimal("1.25")], pa.decimal128(38, 2))})

        out = normalize_leg(leg(("s", 9)), table)

        assert out.schema.field("s").type == pa.decimal128(38, 9)
        assert out.column("s")[0].as_py() == Decimal("1.250000000")

    def test_trailing_zeros_past_the_scale_are_not_a_loss(self) -> None:
        table = pa.table({"s": pa.array([Decimal("1.250000000000")], pa.decimal128(38, 12))})

        out = normalize_leg(leg(("s", 9)), table)

        assert out.column("s")[0].as_py() == Decimal("1.25")

    def test_a_sum_with_more_places_is_refused_not_rounded(self) -> None:
        table = pa.table({"s": pa.array([Decimal("1.0000000001")], pa.decimal128(38, 10))})

        with pytest.raises(FederatedLegError, match="more than 9 decimal places"):
            normalize_leg(leg(("s", 9)), table)

    def test_a_floating_point_sum_is_refused(self) -> None:
        table = pa.table({"s": pa.array([1.5], pa.float64())})

        with pytest.raises(FederatedLegError, match="floating point"):
            normalize_leg(leg(("s", 9)), table)

    def test_an_integer_sum_reads_at_scale_zero(self) -> None:
        table = pa.table({"s": pa.array([2**62, 2**62], pa.int64())})

        out = normalize_leg(leg(("s", 0)), table)

        assert out.schema.field("s").type == pa.decimal128(38, 0)
        assert out.column("s").to_pylist() == [2**62, 2**62]


# (group, key, value): one group per edge of the output type.
EDGES = [
    ("half", 1, "1"),
    ("half", 2, "2"),
    ("negative half", 3, "-1"),
    ("negative half", 4, "-2"),
    ("tenths", 5, "0.1"),
    ("tenths", 6, "0.2"),
    *[("zeros and ones", 7 + n, str(n % 2)) for n in range(20)],
]

EDGE_MODEL = (
    MODEL.split("measures:")[0]
    .replace("      I: {code: i, abstractType: int}\n", "")
    .replace("      F: {code: f, abstractType: float}\n", "")
    + """measures:
  Avg 0: {columns: [{dataObject: Facts, column: D}], aggregation: avg, dataType: "decimal(38, 0)"}
  Avg 37: {columns: [{dataObject: Facts, column: D}], aggregation: avg, dataType: "decimal(38, 37)"}
  Avg 38: {columns: [{dataObject: Facts, column: D}], aggregation: avg, dataType: "decimal(38, 38)"}
  __AVG0_SUM: {columns: [{dataObject: Facts, column: D}], aggregation: count, resultType: int}
"""
)


@pytest.fixture(scope="module")
def edge_files(tmp_path_factory: Any) -> dict[str, str]:
    root = tmp_path_factory.mktemp("avg_edges")
    paths = {"facts": str(root / "facts.duckdb"), "labels": str(root / "labels.duckdb")}
    con = duckdb.connect(paths["facts"])
    con.execute("CREATE TABLE facts (k INTEGER, d DECIMAL(18, 2))")
    con.executemany(
        "INSERT INTO facts VALUES (?, CAST(? AS DECIMAL(18, 2)))", [(k, d) for _, k, d in EDGES]
    )
    con.close()
    con = duckdb.connect(paths["labels"])
    con.execute("CREATE TABLE groups (k INTEGER, g VARCHAR)")
    con.executemany("INSERT INTO groups VALUES (?, ?)", [(k, g) for g, k, _ in EDGES])
    con.close()
    return paths


def run_edge(files: dict[str, str], measures: list[str], group: str) -> dict[str, Any]:
    store = ModelStore()
    model_id = store.load_model(EDGE_MODEL).model_id
    query = QueryObject.model_validate(
        {
            "select": {"dimensions": ["Label"], "measures": measures},
            "where": [{"field": "Label", "op": "=", "value": group}],
        }
    )
    plan = store.compile_query(model_id, query, "duckdb").federation
    assert plan is not None
    tables = {}
    for leg in plan.legs:
        con = duckdb.connect(files[leg.data_source], read_only=True)
        try:
            tables[leg.name] = normalize_leg(leg, con.sql(leg.sql).to_arrow_table())
        finally:
            con.close()
    (row,) = combine(plan, tables, None).arrow_table.to_pylist()
    return row


class TestOutputScaleEdges:
    """Where the measure's type leaves no place, or no integer digit, to spare."""

    @pytest.mark.parametrize(
        ("group", "expected"), [("half", Decimal(2)), ("negative half", Decimal(-2))]
    )
    def test_scale_zero_rounds_half_away_from_zero(
        self, edge_files: dict[str, str], group: str, expected: Decimal
    ) -> None:
        """1.5 is 2 and -1.5 is -2: at no places the quotient rounds itself."""
        assert run_edge(edge_files, ["Avg 0"], group)["Avg 0"] == expected

    def test_a_scale_of_37_does_not_overflow(self, edge_files: dict[str, str]) -> None:
        """Ten zeros and ten ones: one step of 37 digits would scale the
        remainder past 128 bits; two steps of at most 19 do not."""
        assert run_edge(edge_files, ["Avg 37"], "zeros and ones")["Avg 37"] == Decimal("0.5")

    def test_a_scale_of_38_stays_a_type(self, edge_files: dict[str, str]) -> None:
        """The scaling constant is DECIMAL(38, 38), not the DECIMAL(39, 38)
        no engine has."""
        assert run_edge(edge_files, ["Avg 38"], "tenths")["Avg 38"] == Decimal("0.15")

    def test_a_partial_name_differing_only_in_case_is_not_taken(
        self, edge_files: dict[str, str]
    ) -> None:
        """DuckDB reads __AVG0_SUM and __avg0_sum as one identifier."""
        row = run_edge(edge_files, ["Avg 0", "__AVG0_SUM"], "tenths")

        assert row["Avg 0"] == Decimal(0)
        assert row["__AVG0_SUM"] == 2


def _round_half_away(value: Decimal, places: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 120
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _truncate(value: Decimal, places: int) -> Decimal:
    from decimal import ROUND_DOWN

    with localcontext() as ctx:
        ctx.prec = 120
        return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)


def _cases(seed: int, n: int) -> list[tuple[int, int, int, int, int]]:
    """``(input_scale, precision, scale, total_units, count)``, each average fitting its type.

    Totals across the whole DECIMAL(38, input_scale) range and counts up to
    2**63 - 1, in both signs; the type is any of DECIMAL(1, 0) to (38, 38).
    """
    import random

    rng = random.Random(seed)
    cases = []
    while len(cases) < n:
        input_scale = rng.choice([0, 9])
        precision = rng.randint(1, 38)
        scale = rng.randint(0, precision)
        count = rng.choice([rng.randint(1, 9), rng.randint(1, 10**6), rng.randint(1, 2**63 - 1)])
        digits = rng.randint(1, 38)
        units = rng.randint(-(10**digits) + 1, 10**digits - 1)
        with localcontext() as ctx:
            ctx.prec = 120
            mean = Decimal(units).scaleb(-input_scale) / count
            rounded = _round_half_away(mean, scale)
        if abs(rounded) >= Decimal(10) ** (precision - scale):
            continue  # the average does not fit the type: the cast would refuse it anyway
        cases.append((input_scale, precision, scale, units, count))
    return cases


class TestExactQuotientProperty:
    """``exact_quotient`` against exact decimal arithmetic, over the input range."""

    @pytest.mark.parametrize("seed", range(6))
    def test_matches_exact_arithmetic(self, seed: int) -> None:
        from orionbelt.ast.nodes import ColumnRef
        from orionbelt.dialect.duckdb import DuckDBDialect
        from orionbelt.models.types import DecimalType

        dialect = DuckDBDialect()
        con = duckdb.connect()
        for input_scale, precision, scale, units, count in _cases(seed, 100):
            target = DecimalType(precision=precision, scale=scale)
            expr = dialect.exact_quotient(
                ColumnRef(name="t"), ColumnRef(name="c"), input_scale, target
            )
            assert expr is not None
            sql = dialect.compile_expr(expr)
            total = Decimal(units).scaleb(-input_scale)
            raw, cast = con.execute(
                f"SELECT {sql}, CAST({sql} AS DECIMAL({precision}, {scale})) FROM "
                f"(SELECT CAST(? AS DECIMAL(38, {input_scale})) AS t, CAST(? AS BIGINT) AS c)",
                [str(total), count],
            ).fetchone()
            with localcontext() as ctx:
                ctx.prec = 120
                mean = total / count
            case = (input_scale, precision, scale, units, count)
            # The measure's column: rounded once, half away from zero.
            assert cast == _round_half_away(mean, scale), case
            # What HAVING and metrics read: truncated at more places, or
            # rounded where the type has none to spare.
            places = raw.as_tuple().exponent * -1 if raw.as_tuple().exponent < 0 else 0
            if places > scale:
                assert raw == _truncate(mean, places), case
            else:
                assert raw == _round_half_away(mean, places), case

    def test_an_empty_group_is_null(self) -> None:
        from orionbelt.ast.nodes import ColumnRef
        from orionbelt.dialect.duckdb import DuckDBDialect
        from orionbelt.models.types import DecimalType

        dialect = DuckDBDialect()
        sql = dialect.compile_expr(
            dialect.exact_quotient(
                ColumnRef(name="t"), ColumnRef(name="c"), 9, DecimalType(precision=18, scale=2)
            )
        )

        ((value,),) = duckdb.sql(
            f"SELECT {sql} FROM (SELECT CAST(5 AS DECIMAL(38, 9)) AS t, CAST(0 AS BIGINT) AS c)"
        ).fetchall()

        assert value is None

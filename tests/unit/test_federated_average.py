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


def run(files: dict[str, str], measures: list[str]) -> dict[str, dict[str, Any]]:
    store = ModelStore()
    model_id = store.load_model(MODEL).model_id
    query = QueryObject.model_validate({"select": {"dimensions": ["Label"], "measures": measures}})
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


def exact(values: list[Any]) -> Decimal | None:
    present = [Decimal(str(v)) for v in values if v is not None]
    if not present:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        return (sum(present) / len(present)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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

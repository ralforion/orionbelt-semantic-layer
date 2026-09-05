"""Assert the Arrow type OBSL returns for each declared OBML type (II-6).

``scripts/probe_types.py`` measures all eight engines, but it calls
``cursor.fetch_arrow_table()`` directly and so measures the *driver*. That is
the right tool for "is this engine faithful", and the wrong one for "is OBSL
faithful": every defect this project has actually shipped lived between the
driver and the caller, not in the driver.

    #393  MySQL types inferred per page rather than from column metadata
    #407  Snowflake integer widths narrowed by value; empty result raised
    #410  the cache codec typed a column from the values it happened to hold
    #412  the executor never imported pyarrow, so no result carried a schema
          at all and #410 fell back to inference on a default deployment

A driver-level probe is blind to all four. So this goes through
``db_executor.execute_sql`` - the path REST, pgwire and the CLI all use - and
asserts on what a caller receives.

DuckDB is the engine here because it needs no credentials and so runs on every
CI machine. The other seven are measured by the probe and published in
``docs/reference/type-fidelity.md``; keeping them out of CI is deliberate,
since a suite that skips six of eight rows reports green for a matrix nobody
measured.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pytest

from orionbelt.ast.nodes import RawSQL
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.types import parse_data_type
from orionbelt.service.db_executor import execute_sql

#: ``(OBML type, literal, expected Arrow type)``. The expectation is what a
#: caller must receive, which is not always what the engine stores: DuckDB's
#: ``SUM`` widens to ``decimal128(38, 2)``, and that is correct rather than a
#: defect - an aggregate needs headroom the input width does not have.
CASES: list[tuple[str, str, pa.DataType]] = [
    ("decimal(18,2)", "2.55", pa.decimal128(18, 2)),
    ("decimal(38,9)", "2.123456789", pa.decimal128(38, 9)),
    ("integer", "42", pa.int32()),
    ("bigint", "9007199254740993", pa.int64()),
    ("double", "2.5", pa.float64()),
    ("string", "'x'", pa.string()),
    ("boolean", "1", pa.bool_()),
    ("date", "'2026-08-15'", pa.date32()),
]


@pytest.fixture(scope="module")
def duckdb_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real DuckDB file: the executor opens read-only, which in-memory refuses."""
    import duckdb

    path = tmp_path_factory.mktemp("fidelity") / "probe.duckdb"
    duckdb.connect(str(path)).close()
    return path


@pytest.fixture(scope="module")
def duckdb_env(duckdb_file: Path) -> None:
    """Point the executor at that file for the module."""
    previous = os.environ.get("DUCKDB_DATABASE")
    os.environ["DUCKDB_DATABASE"] = str(duckdb_file)
    yield
    if previous is None:
        os.environ.pop("DUCKDB_DATABASE", None)
    else:
        os.environ["DUCKDB_DATABASE"] = previous


def _cast(literal: str, obml_type: str) -> str:
    """*literal* cast to *obml_type* exactly as the compiler would emit it."""
    dialect = DialectRegistry.get("duckdb")
    expr = dialect.cast_to_obml_type(RawSQL(sql=literal), parse_data_type(obml_type))
    return str(dialect.compile_expr(expr))


def _returned_type(obml_type: str, literal: str) -> pa.DataType:
    """The Arrow type a caller receives for *literal* declared as *obml_type*.

    Rendered through the dialect's own ``cast_to_obml_type``, so a change to a
    cast rendering shows up here rather than only in a hand-written SQL string
    that has drifted from what the compiler emits.
    """
    result = execute_sql(f"SELECT {_cast(literal, obml_type)} AS c", dialect="duckdb")
    assert result.arrow_schema is not None, (
        "the executor returned no Arrow schema, so every column downstream is "
        "typed by inference over its values - see #412"
    )
    return result.arrow_schema.field(0).type


@pytest.mark.usefixtures("duckdb_env")
class TestOBSLReturnsTheDeclaredType:
    @pytest.mark.parametrize(
        ("obml_type", "literal", "expected"),
        CASES,
        ids=[c[0] for c in CASES],
    )
    def test_declared_type_survives_to_the_caller(
        self, obml_type: str, literal: str, expected: pa.DataType
    ) -> None:
        assert _returned_type(obml_type, literal) == expected


@pytest.mark.usefixtures("duckdb_env")
class TestKnownWidening:
    """Widening that is correct, pinned so a change to it is deliberate."""

    def test_sum_widens_to_the_engine_maximum(self) -> None:
        """An aggregate needs headroom its input width does not have."""
        assert _returned_type("decimal(18,2)", "2.55") == pa.decimal128(18, 2)
        cast = _cast("2.55", "decimal(18,2)")
        result = execute_sql(f"SELECT SUM({cast}) AS c", dialect="duckdb")
        assert result.arrow_schema is not None
        assert result.arrow_schema.field(0).type == pa.decimal128(38, 2)


# ---------------------------------------------------------------------------
# The #412 regression, which only a clean process can see
# ---------------------------------------------------------------------------

#: Run in a subprocess because pytest has already imported pyarrow, and that
#: is precisely the condition that hid the bug: every in-process assertion
#: above passes whether or not ``ensure_arrow`` does anything. A deployment
#: does not have pytest's imports, so the guard has to be checked somewhere
#: that does not either.
_CLEAN_PROCESS_CHECK = """
import sys
assert "pyarrow" not in sys.modules, "this check is meaningless if pyarrow is preloaded"

from orionbelt.service.db_executor import ensure_arrow, execute_sql

ensure_arrow()
result = execute_sql("SELECT CAST(1.5 AS DECIMAL(18,2)) AS c", dialect="duckdb")
assert result.arrow_schema is not None, "no driver schema: see #412"
print(result.arrow_schema.field(0).type)
"""


def test_a_server_start_makes_results_carry_a_driver_schema(duckdb_file: Path) -> None:
    """What a REST or pgwire process does at startup, in a process like theirs.

    Without ``ensure_arrow`` the executor cannot take the driver's Arrow path,
    ``ExecutionResult.arrow_schema`` is ``None``, and every column downstream
    is typed by inference over the values one result happened to contain -
    which is the fix #410 shipped and #412 discovered was not running.
    """
    env = {**os.environ, "DUCKDB_DATABASE": str(duckdb_file)}
    proc = subprocess.run(
        [sys.executable, "-c", _CLEAN_PROCESS_CHECK],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "decimal128(18, 2)"


# ---------------------------------------------------------------------------
# The probe's own contract
# ---------------------------------------------------------------------------


class TestProbeOmitsUnreachableEngines:
    """A published matrix must not show a row nobody measured.

    The per-case retry in ``measure`` exists so one unsupported cast cannot
    blank an engine, but it cannot tell "this cast failed" from "nothing
    answered": on a dead connection it turned a single connection error into a
    full row of ERR cells, and ``main`` never reached its omit branch. That row
    then reads as eleven measured failures - a claim about the engine rather
    than about the connection. It shipped that way for Dremio in the first run
    of this matrix, which was really a stopped container.
    """

    def _probe_module(self):
        import importlib.util

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "probe_types", root / "scripts" / "probe_types.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_measure_raises_rather_than_returning_a_row_of_errors(self) -> None:
        probe = self._probe_module()
        previous = os.environ.pop("DUCKDB_DATABASE", None)
        try:
            with pytest.raises(probe.EngineUnreachableError):
                probe.measure("duckdb")
        finally:
            if previous is not None:
                os.environ["DUCKDB_DATABASE"] = previous

    def test_json_output_omits_the_engine(self) -> None:
        probe = self._probe_module()
        previous = os.environ.pop("DUCKDB_DATABASE", None)
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(probe.__file__)), "--json", "duckdb"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert proc.returncode == 0, proc.stderr
            assert json.loads(proc.stdout)["engines"] == {}
            assert "unreachable: duckdb" in proc.stderr
        finally:
            if previous is not None:
                os.environ["DUCKDB_DATABASE"] = previous


class TestPublishedLabelsMatchWhatIsCast:
    """The matrix's first column is the *declared* type, so a label that
    disagrees with the cast publishes a declaration nobody made. The ``big``
    row said ``decimal(18,2)`` beside a ``CAST(... AS DECIMAL(19, 2))``.
    """

    def test_every_case_label_names_its_declared_type(self) -> None:
        import importlib.util

        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "probe_types", root / "scripts" / "probe_types.py"
        )
        assert spec is not None and spec.loader is not None
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)

        for label, _literal, obml_type, aggregate in probe.CASES:
            stem = label.replace("SUM ", "").replace(" big", "").strip()
            declared = obml_type.replace(" ", "")
            assert stem.replace(" ", "") == declared, (
                f"case {label!r} is cast as {obml_type!r}; the label has to say so"
            )
            assert aggregate == label.startswith("SUM ")

    def test_the_published_matrix_uses_the_same_labels(self) -> None:
        root = Path(__file__).resolve().parents[2]
        matrix = json.loads((root / "scripts" / "type-fidelity-matrix.json").read_text())
        page = (root / "docs" / "reference" / "type-fidelity.md").read_text()
        for row in matrix["engines"].values():
            for label in row:
                assert f"`{label}`" in page, f"{label!r} measured but not published"

"""``db_executor.ensure_arrow`` and the entry points that must call it.

The executor's Arrow fetch runs only when pyarrow is already imported, and
whether it is decided whether a result carried the driver's schema. Without
that schema ``build_result_table`` types a column from the values one result
happened to contain, which is the instability #410 was written to remove - so
#410 only held where something unrelated had already imported pyarrow. The
one thing that did was a cache warm-up gated on ``cache_backend == "file"``,
and that setting defaults to ``noop``.

These tests pin the contract rather than the import: that the helper reports
availability, and that every surface which executes SQL calls it.
"""

from __future__ import annotations

import ast
import pathlib
from decimal import Decimal

from orionbelt.service.db_executor import ensure_arrow

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "orionbelt"


def _calls_ensure_arrow(relative: str) -> bool:
    """Whether the module names ``ensure_arrow`` in a call position."""
    tree = ast.parse((SRC / relative).read_text())
    return any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "ensure_arrow")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "ensure_arrow")
        )
        for node in ast.walk(tree)
    )


class TestEnsureArrow:
    def test_reports_availability(self) -> None:
        assert ensure_arrow() is True  # pyarrow is a test dependency

    def test_is_idempotent(self) -> None:
        assert ensure_arrow() is True
        assert ensure_arrow() is True

    def test_makes_the_arrow_fetch_path_reachable(self) -> None:
        """The guard the helper exists to satisfy."""
        import sys

        ensure_arrow()
        assert "pyarrow" in sys.modules


class TestEveryExecutingSurfaceCallsIt:
    """A surface that executes SQL without this returns inferred types.

    Asserted structurally because the failure is silent: nothing errors, the
    columns simply come back typed from their values. A new surface that
    forgets the call would otherwise ship the same regression unnoticed.
    """

    def test_rest_api_calls_it_at_startup(self) -> None:
        assert _calls_ensure_arrow("api/app.py")

    def test_pgwire_calls_it_before_accepting_connections(self) -> None:
        assert _calls_ensure_arrow("pgwire/server.py")

    def test_cli_calls_it_before_executing(self) -> None:
        assert _calls_ensure_arrow("cli/_local.py")

    def test_datasource_probe_calls_it(self) -> None:
        assert _calls_ensure_arrow("service/datasource_probe.py")

    def test_api_startup_call_is_not_gated_on_the_cache_backend(self) -> None:
        """The regression itself: the call used to sit inside
        ``if cache.backend_name == "file":`` and so never ran on the default
        ``cache_backend=noop``."""
        source = (SRC / "api" / "app.py").read_text()
        before_cache_branch = source.split('if cache.backend_name == "file":')[0]
        assert "ensure_arrow()" in before_cache_branch


class TestTheTypeThisProtects:
    """What the carried schema is worth, in the terms a user sees."""

    def test_a_wide_declaration_survives_narrow_values(self) -> None:
        import pyarrow as pa

        from orionbelt.cache.result_codec import build_result_table

        rows = [[Decimal("1.50")], [Decimal("2.25")]]
        schema = pa.schema([pa.field("amount", pa.decimal128(18, 2))])

        inferred = build_result_table(["amount"], rows, None)
        carried = build_result_table(["amount"], rows, schema)

        # Inference reads only the values present, so the same column comes
        # back a different width for a different filter.
        assert inferred.schema.field(0).type == pa.decimal128(3, 2)
        assert carried.schema.field(0).type == pa.decimal128(18, 2)

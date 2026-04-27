"""Tests for query-level coalesce dimensions.

Covers resolution validation, CFL outer-wrapper SQL emission, and
type-mismatch / alias-collision error paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import (
    CoalesceDimension,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_MODEL_PATH = Path(__file__).resolve().parents[2] / "examples" / "sem-layer.obml.yml"


def _load_model():
    raw, src = TrackedLoader().load(_MODEL_PATH)
    model, _ = ReferenceResolver().resolve(raw, src)
    return model


class TestCoalesceCompilation:
    def test_coalesce_emits_in_outer_select_and_group_by(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Employee Name", "Purchase Employee"], alias="Employee"
                    )
                ],
                measures=["Total Sales", "Total Purchase Qty"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        sql = result.sql

        # Outer wrapper coalesces the two role-playing dims into one alias
        assert 'COALESCE("Employee Name", "Purchase Employee") AS "Employee"' in sql
        # GROUP BY uses the same expression (portable across dialects)
        assert 'GROUP BY COALESCE("Employee Name", "Purchase Employee")' in sql

    def test_order_by_coalesce_alias(self) -> None:
        # ORDER BY by the coalesce alias must work — most dialects accept
        # ordering by an alias from the SELECT.
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Employee Name", "Purchase Employee"], alias="Employee"
                    )
                ],
                measures=["Total Sales", "Total Purchase Qty"],
            ),
            order_by=[QueryOrderBy(field="Employee", direction=SortDirection.ASC)],
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        assert 'ORDER BY "Employee"' in result.sql

    def test_each_leg_still_projects_only_its_own_role(self) -> None:
        # The leg-level fix (via-aware projection) must still hold even when
        # the outer wrapper coalesces.
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Employee Name", "Purchase Employee"], alias="Employee"
                    )
                ],
                measures=["Total Sales", "Total Purchase Qty"],
            )
        )
        result = CompilationPipeline().compile(query, model, "postgres")
        # Leg 1 (Sales): Purchase Employee projects as NULL
        assert 'CAST(NULL AS VARCHAR) AS "Purchase Employee"' in result.sql
        # Leg 2 (Purchases): Employee Name projects as NULL
        assert 'CAST(NULL AS VARCHAR) AS "Employee Name"' in result.sql


class TestCoalesceValidation:
    def test_too_few_members(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[CoalesceDimension(coalesce=["Employee Name"], alias="Solo")],
                measures=["Total Sales"],
            )
        )
        with pytest.raises(ResolutionError) as exc:
            CompilationPipeline().compile(query, model, "postgres")
        assert "COALESCE_TOO_FEW_MEMBERS" in {e.code for e in exc.value.errors}

    def test_alias_collision_with_existing_dimension(self) -> None:
        model = _load_model()
        # "Employee Name" is already a model dimension — using it as an alias
        # must be rejected.
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Employee Name", "Purchase Employee"],
                        alias="Employee Name",
                    )
                ],
                measures=["Total Sales"],
            )
        )
        with pytest.raises(ResolutionError) as exc:
            CompilationPipeline().compile(query, model, "postgres")
        assert "COALESCE_ALIAS_COLLISION" in {e.code for e in exc.value.errors}

    def test_duplicate_alias_in_same_query(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=[
                    CoalesceDimension(
                        coalesce=["Employee Name", "Purchase Employee"], alias="Employee"
                    ),
                    CoalesceDimension(
                        coalesce=["Return Employee", "Shipment Employee"], alias="Employee"
                    ),
                ],
                measures=["Total Sales"],
            )
        )
        with pytest.raises(ResolutionError) as exc:
            CompilationPipeline().compile(query, model, "postgres")
        assert "DUPLICATE_COALESCE_ALIAS" in {e.code for e in exc.value.errors}

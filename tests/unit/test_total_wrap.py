"""Tests for the total (grand total) wrapper CTE logic."""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import (
    AliasedExpr,
    BinaryOp,
    ColumnRef,
    From,
    FunctionCall,
    OrderByItem,
    Select,
    WindowFunction,
)
from orionbelt.compiler.resolution import ResolvedDimension, ResolvedMeasure, ResolvedQuery
from orionbelt.compiler.total_wrap import wrap_with_totals


def _make_dim(name: str = "Country", object_name: str = "Customers") -> ResolvedDimension:
    return ResolvedDimension(
        name=name,
        object_name=object_name,
        column_name=name,
        source_column=name.upper(),
    )


def _make_measure(
    name: str = "Revenue",
    aggregation: str = "sum",
    total: bool = False,
) -> ResolvedMeasure:
    return ResolvedMeasure(
        name=name,
        aggregation=aggregation,
        expression=FunctionCall(
            name=aggregation.upper(), args=[ColumnRef(name="AMOUNT", table="Orders")]
        ),
        total=total,
    )


def _make_ast(
    dim_name: str = "Country",
    measure_names: list[str] | None = None,
    order_by: list[OrderByItem] | None = None,
    limit: int | None = None,
) -> Select:
    """Build a simple planner-output AST."""
    if measure_names is None:
        measure_names = ["Revenue"]
    columns: list[AliasedExpr] = [
        AliasedExpr(expr=ColumnRef(name="COUNTRY", table="Customers"), alias=dim_name),
    ]
    for mname in measure_names:
        columns.append(
            AliasedExpr(
                expr=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
                alias=mname,
            )
        )
    return Select(
        columns=columns,
        from_=From(source="WAREHOUSE.PUBLIC.ORDERS", alias="Orders"),
        group_by=[ColumnRef(name="COUNTRY", table="Customers")],
        order_by=order_by or [],
        limit=limit,
    )


class TestNoTotals:
    def test_returns_ast_unchanged(self) -> None:
        ast = _make_ast()
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(total=False)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        assert result is ast  # identity — no wrapping


class TestSingleTotalMeasure:
    def test_wraps_with_cte(self) -> None:
        ast = _make_ast(measure_names=["Grand Total Revenue"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Grand Total Revenue", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        # Should have a CTE named "base"
        assert len(result.ctes) == 1
        assert result.ctes[0].name == "base"
        # Outer query should SELECT from "base"
        assert result.from_ is not None
        assert result.from_.source == "base"
        # Outer should not have GROUP BY
        assert result.group_by == []

    def test_outer_has_window_function(self) -> None:
        ast = _make_ast(measure_names=["Grand Total Revenue"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Grand Total Revenue", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        # Second column should be AliasedExpr with WindowFunction
        assert len(result.columns) == 2
        measure_col = result.columns[1]
        assert isinstance(measure_col, AliasedExpr)
        assert measure_col.alias == "Grand Total Revenue"
        assert isinstance(measure_col.expr, WindowFunction)
        assert measure_col.expr.func_name == "SUM"
        assert measure_col.expr.partition_by == []


class TestTotalWithRegularMeasure:
    def test_mixed_measures(self) -> None:
        ast = _make_ast(measure_names=["Revenue", "Grand Total Revenue"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[
                _make_measure(name="Revenue", total=False),
                _make_measure(name="Grand Total Revenue", total=True),
            ],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        assert len(result.ctes) == 1
        # 3 columns: dim + regular measure + total measure
        assert len(result.columns) == 3
        # Regular measure: ColumnRef pass-through
        regular = result.columns[1]
        assert isinstance(regular, AliasedExpr)
        assert regular.alias == "Revenue"
        assert isinstance(regular.expr, ColumnRef)
        # Total measure: WindowFunction
        total = result.columns[2]
        assert isinstance(total, AliasedExpr)
        assert total.alias == "Grand Total Revenue"
        assert isinstance(total.expr, WindowFunction)


class TestMetricWithTotalComponent:
    def test_metric_decomposition(self) -> None:
        """Metric with total component is decomposed into components in base CTE."""
        comp_revenue = ResolvedMeasure(
            name="Revenue",
            aggregation="sum",
            expression=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
            total=False,
        )
        comp_grand = ResolvedMeasure(
            name="Grand Total Revenue",
            aggregation="sum",
            expression=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
            total=True,
        )
        metric = ResolvedMeasure(
            name="Revenue Share",
            aggregation="",
            expression=BinaryOp(
                left=ColumnRef(name="Revenue"),
                op="/",
                right=ColumnRef(name="Grand Total Revenue"),
            ),
            component_measures=["Revenue", "Grand Total Revenue"],
            is_expression=True,
        )

        ast = _make_ast(measure_names=["Revenue Share"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[metric],
            base_object="Orders",
            metric_components={
                "Revenue": comp_revenue,
                "Grand Total Revenue": comp_grand,
            },
        )
        result = wrap_with_totals(ast, resolved)
        assert len(result.ctes) == 1
        # Outer should have: dim + metric
        assert len(result.columns) == 2
        metric_col = result.columns[1]
        assert isinstance(metric_col, AliasedExpr)
        assert metric_col.alias == "Revenue Share"
        # The metric expression should contain a WindowFunction for the total component
        assert isinstance(metric_col.expr, BinaryOp)
        # Left: Revenue (non-total) → ColumnRef
        assert isinstance(metric_col.expr.left, ColumnRef)
        assert metric_col.expr.left.name == "Revenue"
        # Right: Grand Total Revenue (total) → WindowFunction
        assert isinstance(metric_col.expr.right, WindowFunction)
        assert metric_col.expr.right.func_name == "SUM"


class TestOrderByRemapping:
    def test_order_by_remapped_to_alias(self) -> None:
        ast = _make_ast(
            measure_names=["Grand Total Revenue"],
            order_by=[
                OrderByItem(
                    expr=ColumnRef(name="COUNTRY", table="Customers"),
                    desc=False,
                ),
            ],
        )
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Grand Total Revenue", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        assert len(result.order_by) == 1
        # Should be alias-only ColumnRef (no table)
        assert isinstance(result.order_by[0].expr, ColumnRef)
        assert result.order_by[0].expr.table is None
        assert result.order_by[0].expr.name == "Country"


class TestLimitOnOuter:
    def test_limit_on_outer_not_base(self) -> None:
        ast = _make_ast(measure_names=["Grand Total Revenue"], limit=10)
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Grand Total Revenue", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        assert result.limit == 10
        # Base CTE should not have limit
        base_cte = result.ctes[-1]
        assert isinstance(base_cte.query, Select)
        assert base_cte.query.limit is None


class TestExistingCTEsPreserved:
    def test_preserves_existing_ctes(self) -> None:
        from orionbelt.ast.nodes import CTE

        existing_cte = CTE(
            name="filtered",
            query=Select(columns=[ColumnRef(name="x")], from_=From(source="t")),
        )
        ast = Select(
            columns=[
                AliasedExpr(
                    expr=ColumnRef(name="COUNTRY", table="Customers"),
                    alias="Country",
                ),
                AliasedExpr(
                    expr=FunctionCall(name="SUM", args=[ColumnRef(name="AMOUNT", table="Orders")]),
                    alias="Grand Total Revenue",
                ),
            ],
            from_=From(source="WAREHOUSE.PUBLIC.ORDERS", alias="Orders"),
            group_by=[ColumnRef(name="COUNTRY", table="Customers")],
            ctes=[existing_cte],
        )
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Grand Total Revenue", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        # Should have existing CTE + new "base" CTE
        assert len(result.ctes) == 2
        assert result.ctes[0].name == "filtered"
        assert result.ctes[1].name == "base"


class TestReaggMapping:
    """Test the re-aggregation mapping for different aggregation types."""

    def test_count_reagg_is_sum(self) -> None:
        ast = _make_ast(measure_names=["Total Count"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Total Count", aggregation="count", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        total_col = result.columns[1]
        assert isinstance(total_col, AliasedExpr)
        assert isinstance(total_col.expr, WindowFunction)
        assert total_col.expr.func_name == "SUM"  # COUNT → SUM reagg

    def test_min_reagg_is_min(self) -> None:
        ast = _make_ast(measure_names=["Global Min"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Global Min", aggregation="min", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        total_col = result.columns[1]
        assert isinstance(total_col, AliasedExpr)
        assert isinstance(total_col.expr, WindowFunction)
        assert total_col.expr.func_name == "MIN"

    def test_max_reagg_is_max(self) -> None:
        ast = _make_ast(measure_names=["Global Max"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Global Max", aggregation="max", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        total_col = result.columns[1]
        assert isinstance(total_col, AliasedExpr)
        assert isinstance(total_col.expr, WindowFunction)
        assert total_col.expr.func_name == "MAX"

    def test_avg_reagg_is_sum_div_count(self) -> None:
        """AVG total uses SUM(sum_helper) / SUM(count_helper) for correctness."""
        ast = _make_ast(measure_names=["Grand Avg"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Grand Avg", aggregation="avg", total=True)],
            base_object="Orders",
        )
        result = wrap_with_totals(ast, resolved)
        total_col = result.columns[1]
        assert isinstance(total_col, AliasedExpr)
        # AVG total: SUM(sum_helper) OVER () / SUM(count_helper) OVER ()
        assert isinstance(total_col.expr, BinaryOp)
        assert total_col.expr.op == "/"
        assert isinstance(total_col.expr.left, WindowFunction)
        assert total_col.expr.left.func_name == "SUM"
        assert isinstance(total_col.expr.right, WindowFunction)
        assert total_col.expr.right.func_name == "SUM"

    @pytest.mark.parametrize("agg", ["median", "mode", "listagg", "any_value"])
    def test_unsupported_total_raises(self, agg: str) -> None:
        """MODE, LISTAGG, and ANY_VALUE cannot be used with total: true."""
        ast = _make_ast(measure_names=["Bad Total"])
        resolved = ResolvedQuery(
            dimensions=[_make_dim()],
            measures=[_make_measure(name="Bad Total", aggregation=agg, total=True)],
            base_object="Orders",
        )
        with pytest.raises(ValueError, match="does not support total"):
            wrap_with_totals(ast, resolved)

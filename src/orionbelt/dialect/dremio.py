"""Dremio dialect implementation."""

from __future__ import annotations

from orionbelt.ast.nodes import Cast, Expr, FunctionCall, Literal
from orionbelt.dialect.base import Dialect, DialectCapabilities, UnsupportedAggregationError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.semantic import TimeGrain


@DialectRegistry.register
class DremioDialect(Dialect):
    """Dremio dialect — reduced function surface, quoting differences."""

    @property
    def name(self) -> str:
        return "dremio"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(
            supports_cte=True,
            supports_qualify=False,
            supports_arrays=False,
            supports_window_filters=False,
            supports_ilike=False,
            # ``measure`` is Databricks Metric View specific.
            unsupported_aggregations=["mode", "measure"],
        )

    def format_table_ref(self, database: str, schema: str, code: str) -> str:
        """Dremio: supports multi-level paths via the ``code`` field.

        Dremio namespaces can be arbitrarily deep (Space.Folder.SubFolder.Table).
        When ``database`` and ``schema`` are empty, ``code`` is used as the full
        path (user encodes the complete Dremio path in the OBML ``code`` field).
        Otherwise falls back to the standard 3-part format.
        All components are quoted to prevent SQL injection.

        Unlike the other three-part dialects this does *not* refuse a
        ``database`` with no ``schema``: a Dremio path has no fixed arity, so
        ``"PROD"."sales"`` is a well-formed two-level path naming exactly what
        the model named. Elsewhere the same pair would silently be read as
        ``schema.table`` - see
        :class:`~orionbelt.dialect.base.AmbiguousTableReferenceError`.
        """
        parts = [self.quote_identifier(p) for p in (database, schema) if p]
        if parts:
            return f"{'.'.join(parts)}.{self.quote_identifier(code)}"
        return self.quote_identifier(code)

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr:
        return FunctionCall(name="DATE_TRUNC", args=[Literal.string(grain.value), column])

    def render_cast(self, expr: Expr, target_type: str) -> Expr:
        return Cast(expr=expr, type_name=target_type)

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr:
        from orionbelt.ast.nodes import BinaryOp

        return BinaryOp(
            left=FunctionCall(name="LOWER", args=[column]),
            op="LIKE",
            right=BinaryOp(
                left=BinaryOp(
                    left=Literal.string("%"),
                    op="||",
                    right=FunctionCall(name="LOWER", args=[pattern]),
                ),
                op="||",
                right=Literal.string("%"),
            ),
        )

    def _render_concat(self, args: list[Expr]) -> str:
        """Dremio's ``CONCAT`` ignores NULL arguments, where the catalog says
        NULL propagates.

        The NULL-guard form rather than a ``||`` chain: Dremio documents
        ``CONCAT`` as NULL-ignoring and says nothing about ``||``, and there is
        no Dremio instance in the test matrix to settle it. The guard gives the
        catalog's answer either way.
        """
        return self._render_concat_null_guard(args)

    def _render_trunc(self, args: list[Expr]) -> str:
        """Dremio spells it ``TRUNCATE`` and documents it as truncating toward
        zero; it has no ``TRUNC``.
        """
        value = self.compile_expr(args[0])
        digits = self.compile_expr(args[1]) if len(args) > 1 else "0"
        return f"TRUNCATE({value}, {digits})"

    def _render_div(self, args: list[Expr]) -> str:
        """Dremio has no integer-division function or operator.

        The quotient is promoted to a float first, because whether Dremio reads
        ``7 / 2`` as integer division is not something this repo can run and
        check, and a floored integer division could not be corrected
        afterwards. With the promotion the rewrite is right either way.
        """
        left = self.compile_expr(args[0], _parent_prec=self._PREC_MUL)
        right = self.compile_expr(args[1], _parent_prec=self._PREC_MUL + 1)
        quotient = f"({left} * 1.0 / {right})"
        return f"SIGN({quotient}) * FLOOR(ABS({quotient}))"

    def _render_log(self, args: list[Expr]) -> str:
        """Dremio's ``LOG`` accepts a base, but its reference does not state
        which argument carries it, and there is no Dremio in the execution
        matrix to settle it. The base change through ``LOG10`` depends on no
        argument order at all.
        """
        base = self.compile_expr(args[0])
        value = self.compile_expr(args[1])
        return f"LOG10({value}) / LOG10({base})"

    def _render_extremum(self, name: str, args: list[Expr]) -> str:
        """Dremio's NULL handling in ``GREATEST`` / ``LEAST`` is unverified
        here, and its ``CONCAT`` already skips NULLs, so the guard applies the
        catalog's rule without depending on which behaviour it has.
        """
        return self._render_null_guard(self._render_named_function(name, args), args)

    def _compile_mode(self, args: list[Expr]) -> str:
        """Dremio does not support MODE aggregation."""
        raise UnsupportedAggregationError("dremio", "mode")

    def current_date_sql(self) -> str:
        return "CURRENT_DATE"

    def date_add_sql(self, date_sql: str, unit: str, count: int) -> str:
        # TIMESTAMPADD (not DATE_ADD + INTERVAL) because Dremio/Calcite interval
        # qualifiers are limited to YEAR/MONTH/DAY/HOUR/MINUTE/SECOND — QUARTER
        # and WEEK are rejected as ``INTERVAL '-1' QUARTER`` but accepted as a
        # TIMESTAMPADD unit. CAST back to DATE to preserve DATE typing (matches
        # the forward spine in render_date_spine_cte_sql).
        return f"CAST(TIMESTAMPADD({unit.upper()}, {count}, {date_sql}) AS DATE)"

    def render_date_trunc_sql(self, column_sql: str, grain: str) -> str:
        return f"DATE_TRUNC('{grain}', {column_sql})"

    def render_pop_previous_value_sql(self, prev_sql: str, current_sql: str) -> str:
        # Dremio miscompiles a ``previousValue`` projection that reads *only* the
        # self-joined ``pop_prev`` alias: its executor reads the joined decimal's
        # bytes as the output date column, raising "Value <garbage> for
        # monthOfYear must be in the range [1,12]". Comparisons that also touch
        # ``pop_base`` (ratio/difference/percentChange) plan correctly, so add a
        # value-preserving reference to the base measure (``+ 0 * COALESCE(...)``
        # is always exactly 0 and never NULL-poisons the result).
        return f"{prev_sql} + 0 * COALESCE({current_sql}, 0)"

    def render_date_spine_cte_sql(
        self, min_date: str, max_date: str, grain: str, offset: int, offset_grain: str
    ) -> str:
        prev = self.date_add_sql("d", offset_grain, offset)
        grain_upper = grain.upper()
        # Cross-join of three 10-row value sets produces 1000 rows (0-999),
        # enough for any practical date range and grain combination.
        # Uses TIMESTAMPADD instead of WITH RECURSIVE (unsupported by Dremio).
        return (
            f"SELECT d AS spine_date,\n"
            f"       CASE WHEN {prev} >= {min_date}\n"
            f"            THEN {prev} END AS spine_date_prev\n"
            f"FROM (\n"
            f"  SELECT CAST(TIMESTAMPADD({grain_upper}, n, {min_date}) AS DATE) AS d\n"
            f"  FROM (\n"
            f"    SELECT a.n + b.n * 10 + c.n * 100 AS n\n"
            f"    FROM (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) a(n)\n"
            f"    CROSS JOIN (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) b(n)\n"
            f"    CROSS JOIN (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) c(n)\n"
            f"  ) AS nums\n"
            f"  WHERE TIMESTAMPADD({grain_upper}, n, {min_date}) <= {max_date}\n"
            f") AS spine"
        )

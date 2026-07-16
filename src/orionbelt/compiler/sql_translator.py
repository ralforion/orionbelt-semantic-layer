"""Natural-SQL → QueryObject translator.

Accepts BI-style SQL against a per-model virtual table and returns a
:class:`QueryObject` ready for :class:`CompilationPipeline.compile`. Pure
function — no I/O, no Flight imports, no FastAPI imports.

See ``design/PLAN_flight_natural_sql.md`` for the full design. Highlights:

* The model is exposed as **one virtual table** named ``<model_name>``.
  Columns of that table are the union of dimensions + measures + metrics.
* ``SELECT`` projects dim / measure / metric labels (case-insensitive).
* ``WHERE`` predicates on measures are auto-routed to ``HAVING``.
* ``GROUP BY`` is silently ignored (implicit from selected dimensions).
* Trailing ``WITH ROLLUP`` / ``WITH CUBE`` and ``GROUP BY ROLLUP/CUBE(...)``
  set ``query.grouping`` per ``design/PLAN_with_rollup.md``.
* Joins, CTEs, subqueries, ``UNION``, ``SELECT *``, and aggregate calls
  wrapped around a measure are rejected with ``UNSUPPORTED_SQL_FEATURE``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import sqlglot
import sqlglot.expressions as exp
from sqlglot.errors import ParseError

from orionbelt.models.errors import SemanticError
from orionbelt.models.query import (
    FilterOperator,
    Grouping,
    NullsPosition,
    QueryFilter,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
    Subquery,
)
from orionbelt.models.semantic import SemanticModel

__all__ = ["SQLTranslationError", "translate_sql_to_query"]


# Match ``WITH ROLLUP`` / ``WITH CUBE`` in trailing-modifier position: either at
# end of statement (``;?`` then EOL) or right before ORDER BY / LIMIT / OFFSET /
# HAVING / FETCH. The lookahead keeps any trailing clauses intact so they can
# still be parsed by sqlglot.
_TRAILING_CLAUSE = r"(?=\s*(?:;|$|ORDER\s+BY\b|LIMIT\b|OFFSET\b|HAVING\b|FETCH\b))"
_TRAILING_WITH_ROLLUP = re.compile(rf"\bWITH\s+ROLLUP\b{_TRAILING_CLAUSE}", re.IGNORECASE)
_TRAILING_WITH_CUBE = re.compile(rf"\bWITH\s+CUBE\b{_TRAILING_CLAUSE}", re.IGNORECASE)


# Map sqlglot aggregate subclasses to canonical aggregation names matching
# ``Measure.aggregation`` values in the OBML model. ``count_distinct`` is
# handled separately because it shares the ``Count`` class with plain COUNT
# but carries an ``exp.Distinct`` child.
_AGG_CLASS_TO_NAME: dict[type[exp.Expression], str] = {
    exp.Sum: "sum",
    exp.Count: "count",
    exp.Avg: "avg",
    exp.Min: "min",
    exp.Max: "max",
    exp.Median: "median",
}


# Wrapping aggregates that are idempotent over a single-value group — i.e.
# ``agg(x)`` returns ``x`` when applied to a singleton. Measures and metrics
# arrive at the SELECT loop already evaluated at the query's grain (one value
# per group), so any of these wraps is a provably-safe no-op shim. ``COUNT``
# and ``COUNT(DISTINCT ...)`` are NOT idempotent (they return cardinality)
# and remain restricted to the "wrap matches measure's declared aggregation"
# path.
_IDEMPOTENT_WRAPS: frozenset[str] = frozenset({"sum", "min", "max", "avg", "median"})


# Function names recognised as the explicit measure-marker syntax. ``MEASURE``
# is the Snowflake ``SEMANTIC_VIEW`` / Databricks metric-view convention;
# ``AGG`` and ``AGGREGATE`` are accepted as portable aliases for BI tools and
# users coming from Calcite-style proposals.
_MEASURE_MARKER_NAMES: frozenset[str] = frozenset({"MEASURE", "AGG", "AGGREGATE"})


# Map SQL operators (sqlglot AST kinds) to QueryObject FilterOperator values.
_OP_MAP: dict[type[exp.Expression], FilterOperator] = {
    exp.EQ: FilterOperator.EQUALS,
    exp.NEQ: FilterOperator.NOT_EQUALS,
    exp.GT: FilterOperator.GT,
    exp.GTE: FilterOperator.GTE,
    exp.LT: FilterOperator.LT,
    exp.LTE: FilterOperator.LTE,
}


class SQLTranslationError(Exception):
    """Raised when the input SQL cannot be translated into a QueryObject.

    Carries a list of :class:`SemanticError` so the REST/Flight surface can
    return precise per-clause diagnostics (one error per offending
    SELECT/WHERE/ORDER BY item).
    """

    def __init__(self, errors: list[SemanticError]) -> None:
        if not errors:
            errors = [
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message="Unsupported SQL",
                )
            ]
        self.errors = errors
        messages = "; ".join(f"[{e.code}] {e.message}" for e in errors)
        super().__init__(messages)


def translate_sql_to_query(sql: str, model: SemanticModel) -> QueryObject:
    """Translate a SQL string against the model's virtual table to a QueryObject.

    Raises :class:`SQLTranslationError` with at least one diagnostic on
    failure. Diagnostics carry stable error codes documented in the OBSL
    reference (``UNKNOWN_SELECT_ITEM``, ``UNSUPPORTED_SQL_FEATURE``,
    ``UNKNOWN_ORDER_BY_FIELD``, ``INVALID_ORDER_BY_POSITION``).
    """
    errors: list[SemanticError] = []

    grouping = _strip_trailing_grouping(sql)
    cleaned_sql = grouping[0]
    forced_grouping = grouping[1]

    try:
        ast = sqlglot.parse_one(cleaned_sql)
    except ParseError as exc:
        raise SQLTranslationError(
            [
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Could not parse SQL: {exc}",
                )
            ]
        ) from None

    if isinstance(ast, exp.Union):
        raise SQLTranslationError(
            [
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message="UNION / UNION ALL is not supported.",
                )
            ]
        )
    if not isinstance(ast, exp.Select):
        raise SQLTranslationError(
            [
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=(
                        f"Only single SELECT statements are supported; got {type(ast).__name__}."
                    ),
                )
            ]
        )

    _reject_unsupported_structure(ast, errors)

    # Build label lookup tables (case-insensitive). ``effective_measures``
    # includes synthesized ``<object>.count`` measures so a Semantic QL
    # ``SELECT "Sales.count" FROM m`` resolves like any declared measure.
    dim_labels = {label.lower(): label for label in model.dimensions}
    measure_labels = {label.lower(): label for label in model.effective_measures}
    metric_labels = {label.lower(): label for label in model.metrics}

    def classify(name: str) -> str | None:
        key = name.lower()
        if key in dim_labels:
            return "dim"
        if key in measure_labels or key in metric_labels:
            return "measure"
        return None

    def canonical(name: str) -> str:
        key = name.lower()
        if key in dim_labels:
            return dim_labels[key]
        if key in measure_labels:
            return measure_labels[key]
        if key in metric_labels:
            return metric_labels[key]
        return name

    # --- raw mode detection ---
    # If every SELECT item is a qualified "<DataObject>"."<column>"
    # reference, this is OBML raw mode: emit QuerySelect(fields=[...])
    # with no aggregation. The translator branches early — raw mode has
    # different semantics (no measures, no GROUP BY, no HAVING).
    raw_fields = _try_translate_raw_mode(ast, model)
    if raw_fields == "MIXED":
        raise SQLTranslationError(
            [
                SemanticError(
                    code="MIXED_RAW_AND_AGGREGATE_MODE",
                    message=(
                        "SELECT mixes qualified raw-mode columns "
                        '(`"DataObject"."column"`) with bare dim/measure labels. '
                        "Use one form consistently — either all raw or all semantic."
                    ),
                )
            ]
        )
    if isinstance(raw_fields, list):
        return _build_raw_mode_query(
            raw_fields,
            ast,
            model,
            errors,
            distinct_flag=bool(ast.args.get("distinct")),
            forced_grouping=forced_grouping,
        )

    # --- SELECT (aggregate mode) ---
    select_dims: list[str] = []
    select_measures: list[str] = []
    if ast.expressions and any(isinstance(e, exp.Star) for e in ast.expressions):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="SELECT * is not supported. List dimensions and measures explicitly.",
            )
        )
    for item in ast.expressions:
        if isinstance(item, exp.Star):
            continue  # already reported

        # Aggregate-wrap path: SUM("Total Sales"), COUNT(DISTINCT "X"), etc.
        # The wrapping aggregate is accepted only when it matches the
        # measure's declared aggregation; metrics reject any wrap because
        # they're already at the query grain (a derived expression has no
        # single outer aggregate that's correct).
        agg_wrap = _classify_aggregate_wrap(item)
        if agg_wrap is not None:
            agg_name, _is_distinct, inner = agg_wrap
            inner_label = _column_name(inner)
            if inner_label is None:
                errors.append(
                    SemanticError(
                        code="UNSUPPORTED_SQL_FEATURE",
                        message=(f"Aggregate `{item.sql()}` must wrap a single measure label."),
                    )
                )
                continue
            kind = classify(inner_label)
            if kind is None:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_SELECT_ITEM",
                        message=(f"`{inner_label}` is not a measure or metric of this model."),
                        context={"item": inner_label},
                    )
                )
                continue
            if kind != "measure":
                errors.append(
                    SemanticError(
                        code="UNSUPPORTED_SQL_FEATURE",
                        message=(
                            f"Cannot apply aggregate `{agg_name.upper()}` to dimension "
                            f"`{inner_label}`. Reference the dimension directly."
                        ),
                        context={"field": inner_label, "wrap": agg_name},
                    )
                )
                continue
            canon = canonical(inner_label)
            # Determine if the inner is a measure or a metric for the
            # idempotence / matching-aggregation check. ``effective_measures``
            # so a wrap on a synthesized count is validated like a declared one.
            measure_obj = model.effective_measures.get(canon)
            metric_obj = model.metrics.get(canon)
            if metric_obj is not None:
                # Metrics have no declared aggregation — they're derived
                # expressions evaluated at the query's grain. Accept only
                # idempotent wraps (SUM/MIN/MAX/AVG/MEDIAN over a singleton
                # returns the singleton value); reject COUNT family which
                # would change cardinality semantics.
                if agg_name not in _IDEMPOTENT_WRAPS:
                    errors.append(
                        SemanticError(
                            code="UNSUPPORTED_SQL_FEATURE",
                            message=(
                                f"Metric `{canon}` is a derived expression already "
                                "evaluated at the query's grain — applying "
                                f"`{agg_name.upper()}(...)` would change its math. Use bare "
                                f'`"{canon}"`, `MEASURE("{canon}")`, or an idempotent '
                                "wrap (SUM/MIN/MAX/AVG/MEDIAN)."
                            ),
                            context={"metric": canon, "wrap": agg_name},
                        )
                    )
                    continue
                # Idempotent wrap on metric: strip it, emit the metric bare.
                select_measures.append(canon)
                continue
            if measure_obj is not None:
                declared = str(measure_obj.aggregation).lower()
                # Two acceptance paths, both yield the measure's declared
                # aggregation (the wrap is purely syntactic):
                #   1. Matching wrap   — ``COUNT(<count-declared measure>)``,
                #      ``SUM(<sum-declared measure>)``, …
                #   2. Idempotent wrap — any of SUM/MIN/MAX/AVG/MEDIAN over a
                #      per-grain measure value returns that value. Lets BI
                #      tools that go through Calcite (Dremio, Spark, Flink)
                #      satisfy "expression must be aggregated" with a uniform
                #      SUM(...) wrap without having to mirror each measure's
                #      declared aggregation.
                if declared != agg_name and agg_name not in _IDEMPOTENT_WRAPS:
                    errors.append(
                        SemanticError(
                            code="UNSUPPORTED_SQL_FEATURE",
                            message=(
                                f"Measure `{canon}` is declared as `{declared.upper()}` — "
                                f"applying `{agg_name.upper()}` would change its math. "
                                f'Use `{declared.upper()}("{canon}")`, bare `"{canon}"`, '
                                f'`MEASURE("{canon}")`, or an idempotent wrap '
                                "(SUM/MIN/MAX/AVG/MEDIAN)."
                            ),
                            context={
                                "measure": canon,
                                "declared": declared,
                                "wrap": agg_name,
                            },
                        )
                    )
                    continue
            select_measures.append(canon)
            continue

        name = _column_name(item)
        if name is None:
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=(
                        "Only bare column references are supported in SELECT — got "
                        f"`{item.sql()}`. Wrap aggregates by referencing the measure label."
                    ),
                )
            )
            continue
        kind = classify(name)
        if kind == "dim":
            select_dims.append(canonical(name))
        elif kind == "measure":
            select_measures.append(canonical(name))
        else:
            errors.append(
                SemanticError(
                    code="UNKNOWN_SELECT_ITEM",
                    message=(f"`{name}` is not a dimension, measure, or metric of this model."),
                    context={"item": name},
                )
            )

    # --- WHERE / HAVING routing ---
    where_filters: list[QueryFilter] = []
    having_filters: list[QueryFilter] = []
    # Default subject column for ``WHERE EXISTS (...)`` — the planner uses
    # it only to derive the outer data object for join resolution, so any
    # SELECT dim/measure works. Picking the first deterministic candidate.
    exists_subject: str | None = None
    if select_dims:
        exists_subject = select_dims[0]
    elif select_measures:
        exists_subject = select_measures[0]
    if ast.args.get("where") is not None:
        where_expr = ast.args["where"].this
        _split_predicates(
            where_expr,
            classify,
            canonical,
            where_filters,
            having_filters,
            errors,
            exists_subject=exists_subject,
        )

    if ast.args.get("having") is not None:
        having_expr = ast.args["having"].this
        _split_predicates(
            having_expr,
            classify,
            canonical,
            where_filters,
            having_filters,
            errors,
            force_having=True,
        )

    # --- GROUP BY: read grouping marker only ---
    group_node = ast.args.get("group")
    parsed_grouping = _detect_grouping(group_node)
    grouping_value: Grouping | None = parsed_grouping or forced_grouping

    # --- ORDER BY ---
    order_by: list[QueryOrderBy] = []
    aliases_in_select = [*select_dims, *select_measures]
    order_node = ast.args.get("order")
    if order_node is not None:
        for ob in order_node.expressions:
            item = _translate_order_by(ob, aliases_in_select, canonical, classify, errors)
            if item is not None:
                order_by.append(item)

    # --- LIMIT / OFFSET ---
    limit_value: int | None = None
    limit_node = ast.args.get("limit")
    if limit_node is not None:
        try:
            limit_value = int(limit_node.expression.sql())
        except (AttributeError, ValueError):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"LIMIT must be an integer literal — got `{limit_node.sql()}`.",
                )
            )

    offset_value: int | None = None
    offset_node = ast.args.get("offset")
    if offset_node is not None:
        try:
            offset_value = int(offset_node.expression.sql())
        except (AttributeError, ValueError):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"OFFSET must be an integer literal — got `{offset_node.sql()}`.",
                )
            )

    if errors:
        raise SQLTranslationError(errors)

    return QueryObject(
        select=QuerySelect(dimensions=list(select_dims), measures=list(select_measures)),
        where=list(where_filters),
        having=list(having_filters),
        order_by=order_by,
        limit=limit_value,
        offset=offset_value,
        grouping=grouping_value,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL comments in all three vendor-supported syntaxes.

    Recognised forms (collapsed across the 8 OBSL-supported dialects):

    * ``-- ...`` to end of line — universal (Postgres, MySQL, Snowflake,
      ClickHouse, DuckDB, Databricks, BigQuery, Dremio).
    * ``# ...`` to end of line — MySQL / MariaDB / BigQuery.
    * ``/* ... */`` block, may span multiple lines — universal.

    Comments inside string literals (``'...'``) and quoted identifiers
    (``"..."``) are preserved. Required at the entry of OBSQL handling
    because the trailing-modifier regexes (``WITH ROLLUP`` / ``WITH
    CUBE``) need to see what *actually* comes after the modifier — and
    a trailing ``-- ORDER BY ...`` (or ``# ...``) comment would
    otherwise hide the end-of-statement marker, causing the modifier
    to slip past the strip and break sqlglot parsing.

    Block comments are replaced with a single space so ``a/*x*/b``
    doesn't fuse into ``ab``.
    """
    if "--" not in sql and "/*" not in sql and "#" not in sql:
        return sql

    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # Pass through quoted strings and quoted identifiers intact.
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                out.append(c)
                if c == quote:
                    # SQL doubled-quote escape: '' inside '...', "" inside "..."
                    if i + 1 < n and sql[i + 1] == quote:
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                # Backslash escape — accepted by MySQL/ClickHouse/BigQuery; harmless on others
                if c == "\\" and i + 1 < n:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                i += 1
            continue
        # Line comment ``-- ...`` (universal) or ``# ...`` (MySQL/BigQuery).
        is_line_comment = (ch == "-" and i + 1 < n and sql[i + 1] == "-") or ch == "#"
        if is_line_comment:
            i += 2 if ch == "-" else 1
            while i < n and sql[i] != "\n":
                i += 1
            # Keep the newline so downstream parsing still sees the boundary.
            continue
        # Block comment ``/* ... */`` — non-nesting per SQL standard; may
        # span multiple lines (the inner loop just walks past ``\n``).
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i < n - 1 and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_grouping(sql: str) -> tuple[str, Grouping | None]:
    """Detect and strip a bare trailing ``WITH ROLLUP`` / ``WITH CUBE``.

    The flight natural-SQL surface auto-injects GROUP BY, so users can write
    ``WITH ROLLUP``/``WITH CUBE`` directly after the WHERE clause (per the
    rollup plan §"Syntax"). sqlglot's parser requires a ``GROUP BY`` clause
    in front, so we strip the trailing form and translate it into the
    canonical grouping marker. ``GROUP BY ROLLUP(...)`` / ``GROUP BY ... WITH
    ROLLUP`` are also accepted (handled by sqlglot natively).

    Strips SQL comments first so a trailing ``-- comment`` after the
    modifier doesn't hide the end-of-statement marker the trailing
    regex looks for.
    """
    s = _strip_sql_comments(sql).rstrip()
    if _TRAILING_WITH_CUBE.search(s):
        return _TRAILING_WITH_CUBE.sub("", s).rstrip(), Grouping.CUBE
    if _TRAILING_WITH_ROLLUP.search(s):
        return _TRAILING_WITH_ROLLUP.sub("", s).rstrip(), Grouping.ROLLUP
    return s, None


def _detect_grouping(group_node: exp.Group | None) -> Grouping | None:
    """Map a sqlglot Group node into our Grouping enum, or None."""
    if group_node is None:
        return None
    if group_node.args.get("cube"):
        return Grouping.CUBE
    if group_node.args.get("rollup"):
        return Grouping.ROLLUP
    return None


def _reject_unsupported_structure(ast: exp.Select, errors: list[SemanticError]) -> None:
    """Catch joins, CTEs, set ops, qualify clauses, windows. Mutates `errors`."""
    if ast.args.get("joins"):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    "JOIN clauses are not supported. The semantic layer handles joins "
                    "automatically based on the selected dimensions and measures."
                ),
            )
        )
    if ast.args.get("with_") is not None:
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="WITH (CTE) clauses are not supported.",
            )
        )
    if ast.find(exp.Union):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="UNION / UNION ALL is not supported.",
            )
        )
    if ast.find(exp.Subquery):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="Subqueries are not supported.",
            )
        )
    if ast.find(exp.Window):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="Window functions are not supported in OrionBelt Semantic QL.",
            )
        )
    if ast.args.get("qualify") is not None:
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="QUALIFY clauses are not supported.",
            )
        )
    # Aggregate function calls outside the SELECT list (e.g. in WHERE / ORDER BY)
    # remain rejected — the SELECT loop has dedicated handling that matches
    # the wrapping aggregate against each measure's declared aggregation.
    # Tableau-style ``HAVING (COUNT(1) > 0)`` / ``HAVING COUNT(*) > 0``
    # tautologies are skipped — they're a BI-tool idiom for "return zero
    # rows when the table is empty" and we drop them at filter-routing
    # time, see :func:`_atom_to_query_filter`.
    select_aggs = {id(a) for a in ast.expressions if isinstance(a, exp.AggFunc)}
    select_aggs.update(
        id(a.this)
        for a in ast.expressions
        if isinstance(a, exp.Alias) and isinstance(a.this, exp.AggFunc)
    )
    having_aggs = {id(a) for a in _collect_count_tautology_aggs(ast.args.get("having"))}
    for agg in ast.find_all(exp.AggFunc):
        if id(agg) in select_aggs or id(agg) in having_aggs:
            continue
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    f"Aggregate call `{agg.sql()}` outside the SELECT list is not supported. "
                    "Reference the measure label directly."
                ),
            )
        )


def _collect_count_tautology_aggs(having_node: exp.Expression | None) -> list[exp.AggFunc]:
    """Return ``COUNT(*) / COUNT(1)`` aggregates inside a HAVING tautology.

    Tableau decorates aggregation queries with ``HAVING (COUNT(1) > 0)``
    to drop the synthetic row Postgres returns for an empty source —
    when there *is* data the predicate is always true, so we silently
    skip it. Returns the aggregate nodes so the caller can exclude them
    from the "aggregate outside SELECT list" rejection.
    """

    if having_node is None:
        return []
    expr = having_node.this if hasattr(having_node, "this") else having_node
    found: list[exp.AggFunc] = []
    for atom in _flatten_and(expr):
        agg = _match_count_tautology(atom)
        if agg is not None:
            found.append(agg)
    return found


def _flatten_and(expr: exp.Expr) -> list[exp.Expr]:
    """Like ``_walk_and`` but doesn't record errors — read-only."""

    if isinstance(expr, exp.And):
        return [*_flatten_and(expr.left), *_flatten_and(expr.right)]
    if isinstance(expr, exp.Paren):
        return _flatten_and(expr.this)
    return [expr]


def _match_count_tautology(atom: exp.Expr) -> exp.AggFunc | None:
    """Return the COUNT aggregate iff ``atom`` is a tautology like
    ``COUNT(*) > 0`` / ``COUNT(*) >= 1`` / ``COUNT(*) != 0`` — i.e.
    a comparison that's trivially true for every non-empty group.

    Tableau's connect-check emits ``HAVING COUNT(*) > 0`` on every
    aggregate viz; quietly dropping it lets the query reach the
    translator unchanged. Comparisons that are *not* tautological
    (``COUNT(*) < 5``, ``COUNT(*) = 0``, ``COUNT(*) > 10``) are real
    filters and must be preserved — silently dropping them would
    widen the result set with no error, which is exactly the kind of
    silently-wrong-number a semantic layer exists to prevent.
    """

    if not isinstance(atom, exp.Binary):
        return None
    left, right = atom.left, atom.right
    # Either side can be the COUNT; the other must be a literal.
    if isinstance(left, exp.Count) and isinstance(right, exp.Literal):
        if not _is_count_star_or_one(left):
            return None
        return left if _is_nonempty_group_tautology(atom, right, count_on="left") else None
    if isinstance(right, exp.Count) and isinstance(left, exp.Literal):
        if not _is_count_star_or_one(right):
            return None
        return right if _is_nonempty_group_tautology(atom, left, count_on="right") else None
    return None


def _is_nonempty_group_tautology(
    atom: exp.Binary,
    literal: exp.Literal,
    *,
    count_on: str,
) -> bool:
    """True when the comparison is trivially satisfied on any non-empty group.

    For COUNT(*) on the left: ``COUNT(*) > 0``, ``COUNT(*) >= 1``,
    ``COUNT(*) != 0``, ``COUNT(*) <> 0`` are tautologies.
    For COUNT(*) on the right the operands flip:
    ``0 < COUNT(*)``, ``1 <= COUNT(*)``, ``0 != COUNT(*)``,
    ``0 <> COUNT(*)``.
    Anything else (``COUNT(*) < 5``, ``COUNT(*) = 0``, ``COUNT(*) > 10``)
    is a real filter and must be preserved.
    """

    try:
        n = int(str(literal.this))
    except (TypeError, ValueError):
        return False

    # Normalize so we always reason as "COUNT(*) op n".
    if count_on == "right":
        # left = literal, right = COUNT; swap operator to its mirror.
        op_node_for_count_left = _MIRROR_OPS.get(type(atom))
        if op_node_for_count_left is None:
            return False
        op_type = op_node_for_count_left
    else:
        op_type = type(atom)

    if op_type is exp.GT and n == 0:
        return True
    if op_type is exp.GTE and n in (0, 1):
        return True
    return bool(op_type is exp.NEQ and n == 0)


# sqlglot expression types for comparison operators; used to mirror an
# operator when the COUNT(*) appears on the right of the comparison.
_MIRROR_OPS: dict[type[exp.Expr], type[exp.Expr]] = {
    exp.GT: exp.LT,
    exp.GTE: exp.LTE,
    exp.LT: exp.GT,
    exp.LTE: exp.GTE,
    exp.EQ: exp.EQ,
    exp.NEQ: exp.NEQ,
}


def _is_count_star_or_one(count: exp.Count) -> bool:
    """``COUNT(*)`` and ``COUNT(1)`` count every row; treat them the same."""

    inner = count.this
    if isinstance(inner, exp.Star):
        return True
    return isinstance(inner, exp.Literal) and str(inner.this) in {"1", "*"}


def _column_name(node: exp.Expression) -> str | None:
    """Extract the column / identifier name from a SELECT or ORDER BY item.

    Returns ``None`` when the node is not a bare column reference (an
    expression, function call, literal, alias, etc.).

    Also unwraps ``MEASURE(<label>)`` — the explicit measure-marker syntax
    used by Snowflake ``SEMANTIC_VIEW`` and Databricks metric views. Inside
    OBSL's natural-SQL surface ``MEASURE("Total Sales")`` is equivalent to
    bare ``"Total Sales"``: the wrapping is a hint to humans and BI tools
    that "this column is already an aggregate".

    ``AGG(<label>)`` and ``AGGREGATE(<label>)`` are accepted as portable
    aliases — same semantics as ``MEASURE()``.
    """
    if isinstance(node, exp.Alias):
        return _column_name(node.this)
    if isinstance(node, exp.Column):
        return str(node.name)
    if isinstance(node, exp.Identifier):
        return str(node.this)
    if isinstance(node, exp.Cast | exp.TryCast):
        # CAST(<col> AS <type>) is a type coercion the BI tool added —
        # Tableau wraps every dimension in CAST(... AS TEXT). The
        # underlying column is what we resolve; the cast itself is a
        # no-op in the semantic layer because dimensions already have
        # their declared dataType.
        return _column_name(node.this)
    if (
        isinstance(node, exp.Anonymous)
        and str(node.name).upper() in _MEASURE_MARKER_NAMES
        and len(node.expressions) == 1
    ):
        # MEASURE(<label>) / AGG(<label>) / AGGREGATE(<label>) — single arg,
        # must be a bare identifier / column.
        return _column_name(node.expressions[0])
    return None


def _try_translate_raw_mode(ast: exp.Select, model: SemanticModel) -> list[str] | str | None:
    """Detect OBML raw-mode SELECT by qualified column refs.

    Returns:

    * ``list[str]`` of ``"DataObject.column"`` strings — every SELECT item
      is a qualified ``<table>.<column>`` reference whose ``<table>``
      matches a known data object. Translator emits
      ``QuerySelect.fields`` for this list.
    * ``"MIXED"`` — at least one qualified raw-mode column *and* at least
      one bare dim/measure/metric label. Caller raises
      ``MIXED_RAW_AND_AGGREGATE_MODE``.
    * ``None`` — no raw-mode columns detected; caller proceeds with the
      aggregate-mode path.

    Detection rule: a SELECT item is "raw-mode-shaped" iff it's an
    ``exp.Column`` (or aliased Column) with a non-empty ``.table`` part
    that matches a known data-object name or label, **and** the bare
    column name is NOT a known dim/measure/metric (those win
    aggregate-mode classification).
    """
    if not hasattr(model, "data_objects") or not model.data_objects:
        return None

    known_objects: set[str] = set()
    for obj_name, obj in model.data_objects.items():
        known_objects.add(obj_name.lower())
        label = getattr(obj, "name", obj_name) or obj_name
        known_objects.add(str(label).lower())

    bare_aggregate_labels: set[str] = set()
    for label in model.dimensions:
        bare_aggregate_labels.add(label.lower())
    for label in model.measures:
        bare_aggregate_labels.add(label.lower())
    for label in model.metrics:
        bare_aggregate_labels.add(label.lower())

    raw_count = 0
    aggregate_count = 0
    raw_refs: list[str] = []

    for item in ast.expressions:
        if isinstance(item, exp.Star):
            return None  # SELECT * — caller's aggregate-mode path rejects it
        inner = item.this if isinstance(item, exp.Alias) else item
        if isinstance(inner, exp.Column) and inner.table:
            table_lc = inner.table.lower()
            if table_lc in known_objects:
                # Find the canonical (case-preserved) data-object label.
                canonical_obj = None
                for obj_name, obj in model.data_objects.items():
                    label = getattr(obj, "name", obj_name) or obj_name
                    if obj_name.lower() == table_lc or str(label).lower() == table_lc:
                        canonical_obj = str(label) if label else obj_name
                        break
                if canonical_obj is None:
                    canonical_obj = inner.table
                raw_refs.append(f"{canonical_obj}.{inner.name}")
                raw_count += 1
                continue
        # Anything else — check whether it's a bare aggregate-mode label.
        if (
            isinstance(inner, exp.Column)
            and not inner.table
            and inner.name.lower() in bare_aggregate_labels
        ):
            aggregate_count += 1
            continue
        # MEASURE() / AGG() / AGGREGATE() / aggregate wrap / metric reference
        # — all aggregate-mode.
        if (
            isinstance(inner, exp.Anonymous)
            and str(getattr(inner, "name", "")).upper() in _MEASURE_MARKER_NAMES
        ):
            aggregate_count += 1
            continue
        if isinstance(inner, exp.AggFunc):
            aggregate_count += 1
            continue
        # Unknown shape — let aggregate-mode path surface a precise error
        return None

    if raw_count == 0:
        return None
    if aggregate_count > 0:
        return "MIXED"
    return raw_refs


def _build_raw_mode_query(
    raw_refs: list[str],
    ast: exp.Select,
    model: SemanticModel,
    errors: list[SemanticError],
    *,
    distinct_flag: bool,
    forced_grouping: Grouping | None = None,
) -> QueryObject:
    """Translate a raw-mode SELECT (qualified columns) to a QueryObject.

    Raw mode has different semantics than aggregate mode:

    * WHERE accepts qualified ``DataObject.column`` predicates (no measure
      routing, no HAVING).
    * HAVING is rejected (raw mode has no aggregates).
    * GROUP BY is rejected (raw mode emits detail rows).
    * Trailing ``WITH ROLLUP`` / ``WITH CUBE`` is rejected (no grouping).
    * ORDER BY accepts the qualified column refs that appear in SELECT.
    * ``DISTINCT`` is honoured via :class:`QuerySelect.distinct`.

    The resulting ``QueryObject`` flows through
    :class:`CompilationPipeline` exactly as if posted to
    ``/query/execute`` with ``select.fields``.
    """
    # HAVING is illegal in raw mode
    if ast.args.get("having") is not None:
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    "HAVING is not allowed in raw-mode OBSQL — there are no aggregates "
                    "to filter on. Use WHERE on the qualified column."
                ),
            )
        )

    # WHERE — translate qualified predicates into QueryFilter on the
    # `<DataObject>.<column>` field string (compiler accepts this form).
    where_filters: list[QueryFilter] = []
    if ast.args.get("where") is not None:
        where_expr = ast.args["where"].this
        for atom in _walk_and(where_expr, errors):
            f = _atom_to_raw_filter(atom, model, errors)
            if f is not None:
                where_filters.append(f)

    # ORDER BY — accept qualified columns or alias position
    order_by: list[QueryOrderBy] = []
    order_node = ast.args.get("order")
    if order_node is not None:
        for ob in order_node.expressions:
            inner = ob.this
            desc = ob.args.get("desc", False)
            if isinstance(inner, exp.Literal) and inner.is_int:
                pos = int(inner.this)
                if pos < 1 or pos > len(raw_refs):
                    errors.append(
                        SemanticError(
                            code="INVALID_ORDER_BY_POSITION",
                            message=(
                                f"ORDER BY position {pos} is out of range (1-{len(raw_refs)})."
                            ),
                            context={"position": pos},
                        )
                    )
                    continue
                order_by.append(
                    QueryOrderBy(
                        field=raw_refs[pos - 1],
                        direction=SortDirection.DESC if desc else SortDirection.ASC,
                    )
                )
                continue
            if isinstance(inner, exp.Column) and inner.table:
                ref = f"{inner.table}.{inner.name}"
                if ref not in raw_refs:
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_ORDER_BY_FIELD",
                            message=(f"ORDER BY field `{ref}` is not in the SELECT list."),
                            context={"field": ref},
                        )
                    )
                    continue
                order_by.append(
                    QueryOrderBy(
                        field=ref,
                        direction=SortDirection.DESC if desc else SortDirection.ASC,
                    )
                )
                continue
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=(
                        f"ORDER BY in raw-mode OBSQL supports qualified columns or "
                        f"1-based positions only — got `{inner.sql()}`."
                    ),
                )
            )

    # LIMIT / OFFSET
    limit_value: int | None = None
    limit_node = ast.args.get("limit")
    if limit_node is not None:
        try:
            limit_value = int(limit_node.expression.sql())
        except (AttributeError, ValueError):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"LIMIT must be an integer literal — got `{limit_node.sql()}`.",
                )
            )

    offset_value: int | None = None
    offset_node = ast.args.get("offset")
    if offset_node is not None:
        try:
            offset_value = int(offset_node.expression.sql())
        except (AttributeError, ValueError):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"OFFSET must be an integer literal — got `{offset_node.sql()}`.",
                )
            )

    # GROUP BY / WITH ROLLUP — illegal in raw mode
    if ast.args.get("group") is not None:
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    "GROUP BY is not allowed in raw-mode OBSQL — raw mode returns "
                    "detail rows, not aggregates."
                ),
            )
        )
    # Trailing ``WITH ROLLUP`` / ``WITH CUBE`` is stripped pre-parse by
    # ``_strip_trailing_grouping``, so ``ast.args.get("group")`` is None
    # even when the user wrote one. Without this explicit check the
    # grouping clause was silently dropped — the user asked for super-
    # aggregate rows over a detail-row query, which is meaningless. Be
    # symmetric with the GROUP BY rejection above.
    if forced_grouping is not None:
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    f"WITH {forced_grouping.value.upper()} is not allowed in raw-mode "
                    f"OBSQL — raw mode returns detail rows, not aggregates."
                ),
            )
        )

    if errors:
        raise SQLTranslationError(errors)

    return QueryObject(
        select=QuerySelect(fields=list(raw_refs), distinct=distinct_flag),
        where=list(where_filters),
        order_by=order_by,
        limit=limit_value,
        offset=offset_value,
    )


def _atom_to_raw_filter(
    atom: exp.Expr,
    model: SemanticModel,  # noqa: ARG001 — reserved for future column-existence checks
    errors: list[SemanticError],
) -> QueryFilter | None:
    """Translate one raw-mode predicate atom.

    Raw mode predicates target qualified ``<DataObject>.<column>``
    references. The compiler-level filter validator accepts this exact
    form (see ``compiler/raw.py``), so the translator simply propagates
    it through.
    """

    def _qualified_field(node: exp.Expression) -> str | None:
        if isinstance(node, exp.Column) and node.table:
            return f"{node.table}.{node.name}"
        return None

    # IN / NOT IN
    if isinstance(atom, exp.In):
        field = _qualified_field(atom.this)
        if field is None:
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported raw-mode predicate `{atom.sql()}`.",
                )
            )
            return None
        values = [_literal_value(e) for e in atom.expressions]
        if any(v is None for v in values):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"IN list must contain only literals — got `{atom.sql()}`.",
                )
            )
            return None
        return QueryFilter(field=field, op=FilterOperator.IN_LIST, value=values)

    if isinstance(atom, exp.Is):
        field = _qualified_field(atom.this)
        if field is None or not isinstance(atom.expression, exp.Null):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported raw-mode IS predicate `{atom.sql()}`.",
                )
            )
            return None
        return QueryFilter(field=field, op=FilterOperator.IS_NULL)

    if isinstance(atom, exp.Like | exp.ILike):
        field = _qualified_field(atom.this)
        pattern = _literal_value(atom.expression)
        if field is None or pattern is None:
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported raw-mode LIKE predicate `{atom.sql()}`.",
                )
            )
            return None
        return QueryFilter(field=field, op=FilterOperator.LIKE, value=pattern)

    for op_type, op_value in _OP_MAP.items():
        if isinstance(atom, op_type):
            field = _qualified_field(atom.this)
            value = _literal_value(atom.expression)
            if field is None or value is None:
                errors.append(
                    SemanticError(
                        code="UNSUPPORTED_SQL_FEATURE",
                        message=(
                            f"Raw-mode predicate `{atom.sql()}` must have the shape "
                            '`"DataObject"."column" op literal`.'
                        ),
                    )
                )
                return None
            return QueryFilter(field=field, op=op_value, value=value)

    errors.append(
        SemanticError(
            code="UNSUPPORTED_SQL_FEATURE",
            message=f"Unsupported raw-mode predicate `{atom.sql()}`.",
        )
    )
    return None


def _classify_aggregate_wrap(
    node: exp.Expression,
) -> tuple[str, bool, exp.Expression] | None:
    """Detect an aggregate-function wrap and return its kind + inner expression.

    Returns ``(agg_name, is_distinct, inner_node)`` where ``agg_name`` is the
    canonical OBML aggregation value (``"sum"``, ``"count"``, ``"avg"``,
    ``"min"``, ``"max"``, ``"median"``, or ``"count_distinct"``). Returns
    ``None`` when the node is not a recognised aggregate.

    Examples::

        SUM("Sales")             → ("sum", False, Column("Sales"))
        COUNT(DISTINCT "x")      → ("count_distinct", True, Column("x"))
        AVG("Sales")             → ("avg", False, Column("Sales"))
    """
    if isinstance(node, exp.Alias):
        return _classify_aggregate_wrap(node.this)
    agg_name = _AGG_CLASS_TO_NAME.get(type(node))
    if agg_name is None:
        return None
    inner = node.this
    is_distinct = False
    if isinstance(inner, exp.Distinct):
        if not inner.expressions:
            return None
        inner = inner.expressions[0]
        is_distinct = True
        if agg_name == "count":
            agg_name = "count_distinct"
    return agg_name, is_distinct, inner


def _split_predicates(
    expr: exp.Expression,
    classify: Callable[[str], str | None],
    canonical: Callable[[str], str],
    where_filters: list[QueryFilter],
    having_filters: list[QueryFilter],
    errors: list[SemanticError],
    *,
    force_having: bool = False,
    exists_subject: str | None = None,
) -> None:
    """Split an AND-chain of predicates and route each to WHERE or HAVING.

    Top-level ``OR`` and nested groups are rejected as
    ``UNSUPPORTED_SQL_FEATURE`` for now — the semantic-SQL surface keeps
    the predicate shape flat (one column op value per item). Future work
    can lift this restriction by emitting :class:`QueryFilterGroup`.

    ``exists_subject`` is the canonical name of the outer SELECT's first
    dim / measure — used by ``EXISTS (SELECT 1 FROM target)`` predicates
    as the subject column for the planner's join walk.
    """
    for atom in _walk_and(expr, errors):
        if force_having and _match_count_tautology(atom) is not None:
            # ``HAVING COUNT(*) > 0`` / ``COUNT(1) > 0`` — Tableau idiom,
            # silently drop. See :func:`_match_count_tautology`.
            continue
        target = _atom_to_query_filter(
            atom, classify, canonical, errors, exists_subject=exists_subject
        )
        if target is None:
            continue
        item, is_measure = target
        if force_having or is_measure:
            having_filters.append(item)
        else:
            where_filters.append(item)


def _walk_and(expr: exp.Expr, errors: list[SemanticError]) -> list[exp.Expr]:
    """Flatten an AND tree into a list of atomic predicates."""
    if isinstance(expr, exp.And):
        return [
            *_walk_and(expr.left, errors),
            *_walk_and(expr.right, errors),
        ]
    if isinstance(expr, exp.Or):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    "Top-level OR predicates are not supported. Use multiple equality "
                    "filters or rewrite as IN (...)."
                ),
            )
        )
        return []
    if isinstance(expr, exp.Paren):
        return _walk_and(expr.this, errors)
    return [expr]


def _atom_to_query_filter(
    atom: exp.Expr,
    classify: Callable[[str], str | None],
    canonical: Callable[[str], str],
    errors: list[SemanticError],
    *,
    exists_subject: str | None = None,
) -> tuple[QueryFilter, bool] | None:
    """Translate one predicate atom into a (QueryFilter, is_measure) pair."""
    # EXISTS / NOT EXISTS — correlated subquery filter.
    # Accepts ``[NOT] EXISTS (SELECT 1 FROM <DataObject> [WHERE <preds>])``.
    # The outer-row subject column is derived from the SELECT's first
    # dim / measure (``exists_subject``) — the planner uses it only to
    # locate the outer data object for join resolution. Inner WHERE
    # predicates become ``Subquery.filter`` items where ``field`` is a
    # bare column name on the target data object.
    if isinstance(atom, exp.Exists) or (
        isinstance(atom, exp.Not) and isinstance(atom.this, exp.Exists)
    ):
        negated = isinstance(atom, exp.Not)
        exists_node: exp.Exists = atom.this if negated else atom  # type: ignore[assignment]
        return _translate_exists(
            exists_node, negated=negated, exists_subject=exists_subject, errors=errors
        )

    # IN / NOT IN
    if isinstance(atom, exp.In):
        name = _column_name(atom.this)
        if name is None:
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported left-hand side in `{atom.sql()}`.",
                )
            )
            return None
        kind = classify(name)
        if kind is None:
            errors.append(
                SemanticError(
                    code="UNKNOWN_FILTER_FIELD",
                    message=f"`{name}` is not a dimension, measure, or metric of this model.",
                    context={"field": name},
                )
            )
            return None
        op = FilterOperator.IN_LIST
        values = [_literal_value(e) for e in atom.expressions]
        if any(v is None for v in values):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"IN list must contain only literals — got `{atom.sql()}`.",
                )
            )
            return None
        return (
            QueryFilter(field=canonical(name), op=op, value=values),
            kind == "measure",
        )

    # IS NULL / IS NOT NULL
    if isinstance(atom, exp.Is):
        name = _column_name(atom.this)
        if name is None or not isinstance(atom.expression, exp.Null):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported IS predicate `{atom.sql()}`.",
                )
            )
            return None
        kind = classify(name)
        if kind is None:
            errors.append(
                SemanticError(
                    code="UNKNOWN_FILTER_FIELD",
                    message=f"`{name}` is not a known field.",
                    context={"field": name},
                )
            )
            return None
        return (
            QueryFilter(field=canonical(name), op=FilterOperator.IS_NULL),
            kind == "measure",
        )
    if isinstance(atom, exp.Not) and isinstance(atom.this, exp.Is):
        # NOT (x IS NULL) → IS NOT NULL
        inner = atom.this
        name = _column_name(inner.this)
        if name is None or not isinstance(inner.expression, exp.Null):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported NOT predicate `{atom.sql()}`.",
                )
            )
            return None
        kind = classify(name)
        if kind is None:
            errors.append(
                SemanticError(
                    code="UNKNOWN_FILTER_FIELD",
                    message=f"`{name}` is not a known field.",
                    context={"field": name},
                )
            )
            return None
        return (
            QueryFilter(field=canonical(name), op=FilterOperator.IS_NOT_NULL),
            kind == "measure",
        )

    # LIKE / NOT LIKE
    if isinstance(atom, exp.Like | exp.ILike):
        name = _column_name(atom.this)
        pattern = _literal_value(atom.expression)
        if name is None or pattern is None:
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported LIKE predicate `{atom.sql()}`.",
                )
            )
            return None
        kind = classify(name)
        if kind is None:
            errors.append(
                SemanticError(
                    code="UNKNOWN_FILTER_FIELD",
                    message=f"`{name}` is not a known field.",
                    context={"field": name},
                )
            )
            return None
        return (
            QueryFilter(field=canonical(name), op=FilterOperator.LIKE, value=pattern),
            kind == "measure",
        )

    # Comparison ops
    for op_type, op_value in _OP_MAP.items():
        if isinstance(atom, op_type):
            name = _column_name(atom.this)
            value = _literal_value(atom.expression)
            if name is None or value is None:
                errors.append(
                    SemanticError(
                        code="UNSUPPORTED_SQL_FEATURE",
                        message=(
                            f"Unsupported predicate `{atom.sql()}`. Only `column op literal` "
                            "shapes are accepted."
                        ),
                    )
                )
                return None
            kind = classify(name)
            if kind is None:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_FILTER_FIELD",
                        message=f"`{name}` is not a known field.",
                        context={"field": name},
                    )
                )
                return None
            return (
                QueryFilter(field=canonical(name), op=op_value, value=value),
                kind == "measure",
            )

    errors.append(
        SemanticError(
            code="UNSUPPORTED_SQL_FEATURE",
            message=f"Unsupported predicate `{atom.sql()}`.",
        )
    )
    return None


def _translate_exists(
    exists_node: exp.Exists,
    *,
    negated: bool,
    exists_subject: str | None,
    errors: list[SemanticError],
) -> tuple[QueryFilter, bool] | None:
    """Translate ``EXISTS (SELECT 1 FROM <target> [WHERE <preds>])``.

    Inner ``SELECT`` is consumed for two things only:

    * ``FROM <target>`` — names the target data object (single source).
    * ``WHERE <preds>`` — optional predicates on the *target's* columns;
      become ``Subquery.filter`` items.

    ``SELECT`` list, ``GROUP BY``, ``ORDER BY``, ``LIMIT``, and joins on
    the inner query are rejected — the planner derives the correlation
    predicates from the model's existing joins, so the inner shape is
    intentionally constrained.
    """
    if exists_subject is None:
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    "EXISTS / NOT EXISTS requires at least one dimension or measure "
                    "in SELECT — the planner needs an outer subject column to walk "
                    "joins from."
                ),
            )
        )
        return None
    inner = exists_node.this
    if not isinstance(inner, exp.Select):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="EXISTS body must be a SELECT statement.",
            )
        )
        return None
    # Reject inner-side features the planner won't model.
    rejected_arg_names = ("group", "order", "limit", "joins", "having")
    for arg_name in rejected_arg_names:
        if inner.args.get(arg_name):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=(
                        f"EXISTS subquery cannot use `{arg_name.upper()}` — only "
                        "FROM and optional WHERE are accepted."
                    ),
                )
            )
            return None
    from_node = inner.args.get("from_")
    if from_node is None or not isinstance(from_node.this, exp.Table):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    "EXISTS subquery must read a single data object via "
                    "`FROM <DataObject>` (no aliases, no joins, no subqueries)."
                ),
            )
        )
        return None
    target_table = from_node.this
    target_name = target_table.name
    if not target_name:
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="EXISTS subquery FROM is missing a data object name.",
            )
        )
        return None
    # Optional WHERE → Subquery.filter
    sub_filters: list[QueryFilter] = []
    inner_where = inner.args.get("where")
    if inner_where is not None:
        for sub_atom in _walk_and(inner_where.this, errors):
            sf = _atom_to_subquery_filter(sub_atom, errors)
            if sf is not None:
                sub_filters.append(sf)
    op = FilterOperator.NONEXISTS if negated else FilterOperator.EXISTS
    return (
        QueryFilter(
            field=exists_subject,
            op=op,
            subquery=Subquery(data_object=target_name, filter=sub_filters),
        ),
        False,  # never a measure-side filter — EXISTS is row-level
    )


def _atom_to_subquery_filter(atom: exp.Expr, errors: list[SemanticError]) -> QueryFilter | None:
    """Translate a single predicate inside an EXISTS subquery body.

    ``field`` is interpreted as a bare column name on the target data
    object — no model lookup, no canonical mapping. Supports the same
    operator surface as outer predicates minus nested EXISTS (rejected
    by the planner with ``NESTED_SUBQUERY_NOT_SUPPORTED``).
    """
    if isinstance(atom, exp.Exists) or (
        isinstance(atom, exp.Not) and isinstance(atom.this, exp.Exists)
    ):
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message="Nested EXISTS inside an EXISTS body is not supported.",
            )
        )
        return None
    if isinstance(atom, exp.In):
        name = _column_name(atom.this)
        if name is None:
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported left-hand side in subquery `{atom.sql()}`.",
                )
            )
            return None
        values = [_literal_value(e) for e in atom.expressions]
        if any(v is None for v in values):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"IN list in subquery must contain only literals — got `{atom.sql()}`.",
                )
            )
            return None
        return QueryFilter(field=name, op=FilterOperator.IN_LIST, value=values)
    if isinstance(atom, exp.Is):
        name = _column_name(atom.this)
        if name is None or not isinstance(atom.expression, exp.Null):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported IS predicate in subquery `{atom.sql()}`.",
                )
            )
            return None
        return QueryFilter(field=name, op=FilterOperator.IS_NULL)
    if isinstance(atom, exp.Not) and isinstance(atom.this, exp.Is):
        inner_is = atom.this
        name = _column_name(inner_is.this)
        if name is None or not isinstance(inner_is.expression, exp.Null):
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported NOT predicate in subquery `{atom.sql()}`.",
                )
            )
            return None
        return QueryFilter(field=name, op=FilterOperator.IS_NOT_NULL)
    if isinstance(atom, exp.Like | exp.ILike):
        name = _column_name(atom.this)
        pattern = _literal_value(atom.expression)
        if name is None or pattern is None:
            errors.append(
                SemanticError(
                    code="UNSUPPORTED_SQL_FEATURE",
                    message=f"Unsupported LIKE predicate in subquery `{atom.sql()}`.",
                )
            )
            return None
        return QueryFilter(field=name, op=FilterOperator.LIKE, value=pattern)
    for op_type, op_value in _OP_MAP.items():
        if isinstance(atom, op_type):
            name = _column_name(atom.this)
            value = _literal_value(atom.expression)
            if name is None or value is None:
                errors.append(
                    SemanticError(
                        code="UNSUPPORTED_SQL_FEATURE",
                        message=(
                            f"Unsupported predicate in subquery `{atom.sql()}`. "
                            "Only `column op literal` shapes are accepted."
                        ),
                    )
                )
                return None
            return QueryFilter(field=name, op=op_value, value=value)
    errors.append(
        SemanticError(
            code="UNSUPPORTED_SQL_FEATURE",
            message=f"Unsupported predicate in EXISTS subquery `{atom.sql()}`.",
        )
    )
    return None


def _literal_value(expr: exp.Expression) -> str | int | float | bool | None:
    """Extract a Python scalar from a sqlglot literal node, or None for non-literals."""
    if isinstance(expr, exp.Literal):
        if expr.is_int:
            return int(expr.this)
        if expr.is_number:
            return float(expr.this)
        return str(expr.this)
    if isinstance(expr, exp.Boolean):
        return bool(expr.this)
    if isinstance(expr, exp.Null):
        return None
    if isinstance(expr, exp.Neg) and isinstance(expr.this, exp.Literal):
        val = _literal_value(expr.this)
        if isinstance(val, int | float):
            return -val
    return None


def _translate_order_by(
    ob: exp.Expression,
    aliases_in_select: list[str],
    canonical: Callable[[str], str],
    classify: Callable[[str], str | None],
    errors: list[SemanticError],
) -> QueryOrderBy | None:
    """Translate one ORDER BY item.

    Accepts:
      * a bare column / identifier (must match a SELECT alias)
      * a positive integer literal (1-based position into the SELECT list)
      * optional ``ASC`` / ``DESC`` direction
      * optional ``NULLS FIRST`` / ``NULLS LAST`` position
    """
    desc = ob.args.get("desc", False)
    nulls = _nulls_position(ob)
    inner = ob.this

    # Position literal
    if isinstance(inner, exp.Literal) and inner.is_int:
        pos = int(inner.this)
        if pos < 1 or pos > len(aliases_in_select):
            errors.append(
                SemanticError(
                    code="INVALID_ORDER_BY_POSITION",
                    message=(
                        f"ORDER BY position {pos} is out of range (1-{len(aliases_in_select)})."
                    ),
                    context={"position": pos},
                )
            )
            return None
        field = aliases_in_select[pos - 1]
        return QueryOrderBy(field=field, direction=_dir(desc), nulls=nulls)

    name = _column_name(inner)
    if name is None:
        errors.append(
            SemanticError(
                code="UNSUPPORTED_SQL_FEATURE",
                message=(
                    f"ORDER BY only supports bare column references or positions — "
                    f"got `{inner.sql()}`."
                ),
            )
        )
        return None
    if classify(name) is None and name not in aliases_in_select:
        errors.append(
            SemanticError(
                code="UNKNOWN_ORDER_BY_FIELD",
                message=(
                    f"ORDER BY field `{name}` is not in the SELECT list and is not a "
                    "known dimension or measure."
                ),
                context={"field": name},
            )
        )
        return None
    return QueryOrderBy(field=canonical(name), direction=_dir(desc), nulls=nulls)


def _dir(desc: bool) -> SortDirection:
    return SortDirection.DESC if desc else SortDirection.ASC


def _nulls_position(ob: exp.Expression) -> NullsPosition | None:
    """Read the ``NULLS FIRST`` / ``NULLS LAST`` clause off a sqlglot Ordered node.

    sqlglot stores the modifier on ``Ordered.args["nulls_first"]`` as a bool
    (True = NULLS FIRST, False = NULLS LAST). ``None`` means unspecified —
    propagate the dialect default rather than forcing one or the other.
    """
    nf = ob.args.get("nulls_first")
    if nf is None:
        return None
    return NullsPosition.FIRST if nf else NullsPosition.LAST

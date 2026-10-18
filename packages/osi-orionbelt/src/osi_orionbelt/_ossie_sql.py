"""Whether an expression stays inside Apache Ossie's ``OSSIE_SQL_2026`` language.

The export tags an expression ``OSSIE_SQL_2026`` only when every node of it is
in the subset ``core-spec/expression_language.md`` defines, so the tag is a
claim another tool can rely on. The check is an allowlist over the sqlglot
tree: a construct the spec does not list (``MODE``, ``ANY_VALUE``, ``LISTAGG``,
a ``LIKE ... ESCAPE`` clause, a subquery) is a violation, and so is anything
sqlglot parses into a node not named here.
"""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp

# Node types for the constructs the spec lists: references and literals,
# operators, CASE, conditional and string functions, the REQUIRED aggregates,
# percentiles, window functions with their specification, DATE_TRUNC and CAST.
_ALLOWED: tuple[type[exp.Expr], ...] = (
    exp.Column,
    exp.Identifier,
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.Star,
    exp.Paren,
    exp.Neg,
    exp.Not,
    exp.And,
    exp.Or,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Mod,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Between,
    exp.In,
    exp.Is,
    exp.Like,
    exp.ILike,
    exp.Case,
    exp.If,
    exp.Coalesce,
    exp.Nullif,
    exp.Cast,
    exp.DataType,
    exp.Contains,
    exp.StartsWith,
    exp.EndsWith,
    exp.Lower,
    exp.Upper,
    exp.Sum,
    exp.Count,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.Distinct,
    exp.Stddev,
    exp.StddevPop,
    exp.StddevSamp,
    exp.Variance,
    exp.VariancePop,
    exp.Median,
    exp.PercentileCont,
    exp.PercentileDisc,
    exp.WithinGroup,
    exp.Order,
    exp.Ordered,
    exp.Window,
    exp.WindowSpec,
    exp.RowNumber,
    exp.Rank,
    exp.DenseRank,
    exp.Ntile,
    exp.PercentRank,
    exp.CumeDist,
    exp.Lag,
    exp.Lead,
    exp.FirstValue,
    exp.LastValue,
    exp.NthValue,
    exp.DateTrunc,
)

# CAST target types the spec lists, as sqlglot names them.
_CAST_TYPES = {
    exp.DataType.Type.VARCHAR,
    exp.DataType.Type.TEXT,
    exp.DataType.Type.INT,
    exp.DataType.Type.BIGINT,
    exp.DataType.Type.DECIMAL,
    exp.DataType.Type.FLOAT,
    exp.DataType.Type.DOUBLE,
    exp.DataType.Type.BOOLEAN,
    exp.DataType.Type.DATE,
    exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIME,
}

# DATE_TRUNC parts the spec lists.
_TRUNC_PARTS = {"year", "quarter", "month", "week", "day", "hour", "minute", "second"}


def subset_violations(sql: str) -> list[str]:
    """What in *sql* falls outside ``OSSIE_SQL_2026``; empty when nothing does."""
    try:
        tree = sqlglot.parse_one(sql)
    except sqlglot.errors.SqlglotError:
        return ["the expression does not parse"]
    found: list[str] = []
    for node in tree.walk():
        if not isinstance(node, _ALLOWED):
            name = node.name.upper() if isinstance(node, exp.Anonymous) else type(node).__name__
            found.append(f"{name} is not in OSSIE_SQL_2026")
        elif isinstance(node, exp.Distinct) and not isinstance(node.parent, exp.Sum | exp.Count):
            found.append("DISTINCT is defined only for SUM and COUNT")
        elif isinstance(node, exp.Cast) and node.to.this not in _CAST_TYPES:
            found.append(f"CAST to {node.to.sql()} is not in OSSIE_SQL_2026")
        elif isinstance(node, exp.DateTrunc) and node.text("unit").lower() not in _TRUNC_PARTS:
            found.append(f"DATE_TRUNC part '{node.text('unit')}' is not in OSSIE_SQL_2026")
    return found

"""The ``model`` table: a model read as one table by a SQL client.

Both wire surfaces announce, per model, a table named ``model`` whose columns
are the model's dimensions, measures and metrics: the Postgres catalog as
``"<model>"."model"``, the Flight SQL catalog as ``model`` in schema
``<model>``. A client that browses the catalog and then reads that table asks
for those columns, and gets them back as one semantic query.

This module holds the rules both surfaces share, so the announced schema, the
expansion of ``SELECT *`` and the zero-row probe cannot drift apart. They had:
Flight announced the declared measures only, Postgres the synthesized counts
too, and a ``SELECT *`` over Flight answered with column metadata - nine
columns where the announced schema had six.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlglot
import sqlglot.expressions as exp

if TYPE_CHECKING:
    from orionbelt.models.semantic import SemanticModel

#: The name the ``model`` table goes by in every model's schema.
MODEL_TABLE = "model"


def model_table_columns(model: SemanticModel) -> list[str]:
    """The ``model`` table's columns, in order: dimensions, measures, metrics.

    Measures include the synthesized counts (``effective_measures``): they are
    measures a query can name, so a table that leaves them out is not the
    model's table.
    """
    return [*model.dimensions, *model.effective_measures, *model.metrics]


def _parse_select(sql: str) -> exp.Select | None:
    """*sql* as a ``SELECT``, or ``None`` for anything else or unparseable."""
    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.SqlglotError:  # unparseable is "not this shape"
        return None
    return parsed if isinstance(parsed, exp.Select) else None


def _is_lone_plain_star(select: exp.Select) -> bool:
    """Whether the projection is exactly ``*`` or ``t.*``, undecorated.

    ``* EXCLUDE (...)``, ``* REPLACE (...)`` and ``* RENAME (...)`` change the
    columns; expanding them to the plain list would drop the modifier and
    answer a different query, so they are left to the translator's refusal.
    """
    if len(select.expressions) != 1:
        return False
    item = select.expressions[0]
    star = item.this if isinstance(item, exp.Column) else item
    return isinstance(star, exp.Star) and not any(star.args.values())


def _is_false_literal(node: exp.Expr) -> bool:
    """``FALSE``, ``1 = 0`` or ``0 = 1``: the forms BI tools probe with."""
    if isinstance(node, exp.Boolean):
        return node.this is False
    if isinstance(node, exp.EQ):
        sides = {node.left.sql(), node.right.sql()}
        return sides == {"0", "1"}
    return False


def _row_limit(select: exp.Select) -> str | None:
    """The row count a ``LIMIT n`` or ``FETCH FIRST n ROWS ONLY`` asks for, as SQL.

    The two are different nodes: ``Limit`` keeps the count in ``expression``,
    ``Fetch`` in ``count`` - and reading ``expression`` off a ``Fetch`` raised,
    which closed a pgwire client's connection. ``None`` when there is no
    limit, or a ``FETCH`` without a count (one row).
    """
    limit = select.args.get("limit")
    if isinstance(limit, exp.Limit):
        count = limit.expression
    elif isinstance(limit, exp.Fetch):
        count = limit.args.get("count")
    else:
        return None
    return count.sql() if count is not None else None


def is_metadata_probe(sql: str) -> bool:
    """Return ``True`` for ``SELECT *`` column-discovery probes.

    BI tools (Tableau, Power BI, DBeaver, ...) ask "what columns does this
    table have" with ``SELECT * FROM x WHERE 1=0`` or ``SELECT * FROM x LIMIT
    0`` (``FETCH FIRST 0 ROWS ONLY`` is the same probe). The answer is the
    table's column shape and no rows.

    Crucially, the gate is BOTH ``SELECT *`` AND the zero-row clause.
    ``SELECT "Customer Country" FROM commerce LIMIT 0`` has explicit columns
    the BI tool already knows about - it is a semantic query, and routing every
    ``LIMIT 0`` / ``WHERE 1=0`` query away from the translator misroutes it.

    Read from the parsed statement, not the text: a ``/* LIMIT 0 */`` comment
    or a ``'LIMIT 0'`` string literal turned a real query into a probe and
    answered it with no rows.
    """
    select = _parse_select(sql)
    if select is None or not _is_lone_plain_star(select):
        return False
    if _row_limit(select) == "0":
        return True
    where = select.args.get("where")
    if where is None:
        return False
    condition = where.this
    conjuncts = condition.flatten() if isinstance(condition, exp.And) else [condition]
    return any(_is_false_literal(conjunct) for conjunct in conjuncts)


def expand_model_star(sql: str, model: SemanticModel) -> str | None:
    """Rewrite ``SELECT * FROM [<schema>.]model ...`` to name every column.

    The star becomes :func:`model_table_columns`, the same list the catalogs
    announce, so the result has exactly the shape a client bound from the
    catalog. ``WHERE`` / ``ORDER BY`` / ``LIMIT`` are kept. A qualified star
    (``m.*``) counts as one.

    Only a lone star over the ``model`` table is rewritten. ``SELECT *,
    "Total Revenue"`` and ``SELECT * FROM <model>`` (the OBSQL form) are left
    to the translator, which refuses a star: the table is something a client
    can bind to, an OBSQL query names what it wants. Returns ``None`` when the
    statement is not that shape, or the model exposes no columns.
    """
    columns = model_table_columns(model)
    if not columns:
        return None
    parsed = _parse_select(sql)
    if parsed is None or parsed.args.get("joins"):
        return None
    source = parsed.args.get("from_")
    if source is None or not isinstance(source.this, exp.Table):
        return None
    if source.this.name.lower() != MODEL_TABLE:
        return None
    if not _is_lone_plain_star(parsed):
        return None
    parsed.set("expressions", [exp.column(name, quoted=True) for name in columns])
    return parsed.sql(dialect="postgres")

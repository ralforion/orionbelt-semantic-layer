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

import re
from typing import TYPE_CHECKING

import sqlglot
import sqlglot.expressions as exp

if TYPE_CHECKING:
    from orionbelt.models.semantic import SemanticModel

#: The name the ``model`` table goes by in every model's schema.
MODEL_TABLE = "model"

# Tableau (and other Postgres clients) probe column metadata with
# ``SELECT * FROM "schema"."table" WHERE 1=0`` or ``... LIMIT 0``. The answer
# is the table's column shape with no rows; the semantic translator would
# (correctly, on its own terms) reject the ``1=0`` predicate.
_RE_SELECT_STAR = re.compile(r"^\s*select\s+\*", re.IGNORECASE)
_RE_ZERO_ROW_METADATA_PROBE = re.compile(
    r"\bwhere\s+(?:1\s*=\s*0|0\s*=\s*1|false)\b",
    re.IGNORECASE,
)
_RE_LIMIT_ZERO_PROBE = re.compile(r"\blimit\s+0\b", re.IGNORECASE)


def model_table_columns(model: SemanticModel) -> list[str]:
    """The ``model`` table's columns, in order: dimensions, measures, metrics.

    Measures include the synthesized counts (``effective_measures``): they are
    measures a query can name, so a table that leaves them out is not the
    model's table.
    """
    return [*model.dimensions, *model.effective_measures, *model.metrics]


def is_metadata_probe(sql: str) -> bool:
    """Return ``True`` for ``SELECT *`` column-discovery probes.

    BI tools (Tableau, Power BI, DBeaver, ...) ask "what columns does this
    table have" with ``SELECT * FROM x WHERE 1=0`` or ``SELECT * FROM x LIMIT
    0``. The answer is the table's column shape and no rows.

    Crucially, the gate is BOTH ``SELECT *`` AND the zero-row clause.
    ``SELECT "Customer Country" FROM commerce LIMIT 0`` has explicit columns
    the BI tool already knows about - it is a semantic query, and routing every
    ``LIMIT 0`` / ``WHERE 1=0`` query away from the translator misroutes it.
    """
    if _RE_SELECT_STAR.match(sql) is None:
        return False
    return (
        _RE_ZERO_ROW_METADATA_PROBE.search(sql) is not None
        or _RE_LIMIT_ZERO_PROBE.search(sql) is not None
    )


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
    try:
        parsed = sqlglot.parse_one(sql, read="postgres")
    except sqlglot.errors.SqlglotError:  # unparseable is "not this shape"
        return None
    if not isinstance(parsed, exp.Select) or parsed.args.get("joins"):
        return None
    source = parsed.args.get("from_")
    if source is None or not isinstance(source.this, exp.Table):
        return None
    if source.this.name.lower() != MODEL_TABLE:
        return None
    if len(parsed.expressions) != 1 or not parsed.expressions[0].is_star:
        return None
    parsed.set("expressions", [exp.column(name, quoted=True) for name in columns])
    return parsed.sql(dialect="postgres")

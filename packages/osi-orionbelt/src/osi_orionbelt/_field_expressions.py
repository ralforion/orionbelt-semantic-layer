# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""An Ossie field computed by an expression, as an OBML computed column.

An Ossie field's SQL reads columns of its dataset's source: ``amount_cents /
100.0``. OBML quotes a column's ``code`` as one name, so that text written into
``code`` compiled to ``"t"."amount_cents / 100.0"``, a column no table has. OBML
says it with a computed column over sibling columns, ``{amount_cents} / 100.0``,
where each sibling has the physical column as its ``code``.

OBML parses a computed column's expression itself (``compiler/expr_parser.py``)
and refuses the whole model when it cannot, so only what that grammar reads is
converted: arithmetic, comparisons, ``AND`` / ``OR`` / ``NOT``, ``IS NULL``,
``IN``, ``BETWEEN``, ``LIKE``, ``CASE``, literals, and plain function calls. A
SQL ``CAST`` becomes OBML's ``cast(x, 'double')``. Anything else (``||``,
``EXTRACT(... FROM ...)``, a subquery) is reported and left as it was.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlglot import expressions as exp


class NotRepresentableError(Exception):
    """A construct OBML has no form for; the caller keeps what it had."""


def obml_cast_type(to: exp.DataType) -> str | None:
    """The OBML ``cast`` type a SQL cast target names, or ``None``.

    OBML's ``cast(x, 'type')`` takes ``'double'`` or ``'decimal(p, s)'``.
    ``DECIMAL(p)`` has scale 0; a bare ``DECIMAL`` leaves its precision to the
    engine, so it has no OBML type, nor does any other target.
    """
    if to.this in (exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT):
        return "double"
    if to.this == exp.DataType.Type.DECIMAL and to.expressions:
        params = [p.this for p in to.expressions if isinstance(p, exp.DataTypeParam)]
        digits = [p.name for p in params if isinstance(p, exp.Literal) and p.is_int]
        if len(digits) == len(to.expressions) and len(digits) in (1, 2):
            scale = digits[1] if len(digits) == 2 else "0"
            return f"decimal({digits[0]}, {scale})"
    return None


def render_obml(node: exp.Expr, column: Callable[[exp.Column], exp.Expr]) -> str:
    """Render *node* as OBML text, each column written by *column*.

    A SQL ``CAST(x AS DOUBLE)`` becomes OBML's ``cast(x, 'double')``, the only
    cast form OBML parses; a target it has no type for raises
    :class:`NotRepresentableError`.
    """

    def rewrite(n: exp.Expr) -> exp.Expr:
        if isinstance(n, exp.Column):
            return column(n)
        if isinstance(n, exp.Cast):
            target = None if isinstance(n, exp.TryCast) else obml_cast_type(n.to)
            if target is None:
                raise NotRepresentableError(
                    f"{n.sql()}: OBML's cast takes 'double' or 'decimal(p, s)'"
                )
            # A replaced node's children are not visited, so the argument is
            # rewritten here.
            return exp.Anonymous(
                this="cast", expressions=[n.this.transform(rewrite), exp.Literal.string(target)]
            )
        return n

    return node.transform(rewrite).sql()


# The nodes OBML's expression grammar reads, rendered by sqlglot as OBML parses
# them. A function call is written ``NAME(args)``; these render that way, while
# others (EXTRACT, TRIM ... FROM, CONCAT's engine-dependent NULLs) do not.
_OPERATORS = (
    exp.Column,
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.Paren,
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Neg,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.And,
    exp.Or,
    exp.Not,
    exp.Between,
    exp.Case,
    exp.If,
    exp.Cast,
    exp.DataType,
    exp.DataTypeParam,
    exp.Identifier,
)
_FUNCTIONS = (
    exp.Coalesce,
    exp.Nullif,
    exp.Abs,
    exp.Round,
    exp.Floor,
    exp.Ceil,
    exp.Upper,
    exp.Lower,
    exp.Length,
    exp.Greatest,
    exp.Least,
    exp.Anonymous,
)


def unreadable(tree: exp.Expr) -> str | None:
    """Why OBML cannot read *tree* as a computed column, or ``None`` when it can."""
    for node in tree.walk():
        if isinstance(node, (*_OPERATORS, *_FUNCTIONS)):
            continue
        if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
            continue
        if isinstance(node, exp.In) and not node.args.get("query"):
            continue
        if isinstance(node, exp.Like) and isinstance(node.expression, exp.Literal):
            continue
        return f"{type(node).__name__.upper()} ({node.sql()})"
    return None


# ``{Sibling}`` and ``{[Data Object].[Column]}`` in an OBML computed column.
SIBLING_REF = re.compile(r"\{(?!\[)([^{}]+)\}")
QUALIFIED_REF = re.compile(r"\{\[[^\]]+\]\.\[[^\]]+\]\}")

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
from decimal import Decimal

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
        if isinstance(n, exp.Literal) and not n.is_string and _EXPONENT.search(n.this):
            # OBML reads a number as digits and dots: 1e2 is "1" then "e2".
            return exp.Literal.number(format(Decimal(n.this), "f"))
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


def _in_grammar(node: exp.Expr) -> bool:
    """Whether *node* is an operator or predicate OBML's expression grammar has."""
    if isinstance(node, exp.Is):
        return isinstance(node.expression, exp.Null)
    if isinstance(node, exp.In):
        return not node.args.get("query")
    if isinstance(node, exp.Like):
        return isinstance(node.expression, exp.Literal)
    return isinstance(node, _OPERATORS)


def _describe(node: exp.Expr) -> str:
    return f"{type(node).__name__.upper()} ({node.sql()})"


def unreadable(tree: exp.Expr) -> str | None:
    """Why OBML cannot read *tree* as a computed column, or ``None`` when it can."""
    for node in tree.walk():
        if not (isinstance(node, _FUNCTIONS) or _in_grammar(node)):
            return _describe(node)
    return None


def unreadable_operator(tree: exp.Expr) -> str | None:
    """The first operator or predicate in *tree* OBML's expression grammar lacks.

    A measure expression OBML cannot parse stops the whole model loading
    (``INVALID_MEASURE_EXPRESSION``). Its grammar calls any function written
    ``NAME(args)``, so only operators and predicates are checked: ``%``,
    ``||``, ``ILIKE`` and ``IS DISTINCT FROM`` are not in it.
    """
    for node in tree.walk():
        operator = isinstance(node, (exp.Binary, exp.Unary, exp.Predicate))
        call = isinstance(node, exp.Func) and node.sql().startswith(f"{node.sql_name()}(")
        if operator and not call and not _in_grammar(node):
            return _describe(node)
    return None


_EXPONENT = re.compile(r"[eE]")

# ``{Sibling}`` and ``{[Data Object].[Column]}`` in an OBML computed column, as
# OBML reads them (``models/expressions.py``): a sibling name starts with a word
# character and holds no ``}``; a string literal holds no reference.
SIBLING_REF = re.compile(r"\{(\w[^}]*)\}")
QUALIFIED_REF = re.compile(r"\{\[[^\]]+\]\.\[[^\]]+\]\}")
SQL_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
# A name OBML resolves as itself: it strips the name and writes it into
# ``{[Data Object].[name]}``, which ends at the first ``]``; a bare number is
# read as a regex quantifier.
_SIBLING_NAME = re.compile(r"\w[^}\]]*")
_REGEX_QUANTIFIER = re.compile(r"\d+(?:,\d*)?")
# What OBML's tokenizer reads besides literals and references.
_OBML_CHARACTERS = re.compile(r"[\w\s+\-*/(),.=<>!]")
# A number OBML reads as two tokens: 1e2 is "1" then "e2".
_NUMBER_THEN_WORD = re.compile(r"(?<![\w.])\d[\d.]*[A-Za-z_]")


def is_sibling_name(name: str) -> bool:
    """Whether *name* can be written as an OBML sibling placeholder ``{name}``."""
    return (
        name == name.strip()
        and _SIBLING_NAME.fullmatch(name) is not None
        and _REGEX_QUANTIFIER.fullmatch(name) is None
    )


def substitute_outside_strings(
    text: str, pattern: re.Pattern[str], repl: Callable[[re.Match[str]], str]
) -> str:
    """Apply *repl* to *pattern* outside SQL string literals, as OBML does."""
    out: list[str] = []
    last = 0
    for literal in SQL_STRING_LITERAL.finditer(text):
        out.append(pattern.sub(repl, text[last : literal.start()]))
        out.append(literal.group(0))
        last = literal.end()
    out.append(pattern.sub(repl, text[last:]))
    return "".join(out)


def obml_unreadable_text(expression: str) -> str | None:
    """Why OBML's tokenizer would refuse *expression*, or ``None`` when it reads it.

    The last check before a computed column replaces a field's code: a model
    that loaded must not stop loading because of a field no query reads.
    """
    text = SQL_STRING_LITERAL.sub("''", expression)
    text = QUALIFIED_REF.sub("x", text)
    text = SIBLING_REF.sub("x", text)
    if _NUMBER_THEN_WORD.search(text):
        return "a number OBML reads as two tokens"
    stray = sorted(set(_OBML_CHARACTERS.sub("", text.replace("''", ""))))
    if stray:
        return f"characters OBML does not read: {' '.join(stray)}"
    return None

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
"""Apache Ossie's date functions, parsed in Ossie's argument order.

Ossie's ``DATEADD(part, amount, date)``, ``DATEDIFF(part, start, end)`` and
``DATE_PART('part', date)`` take the date part first, as Snowflake's do.
sqlglot's default grammar, which the import reads ANSI SQL and Ossie SQL with,
reads a bare part (``day``) as a column, so a dataset-scoped metric qualified it
like one (``t.day``) and the metric was preserved; and it reads ``DATEDIFF``'s
arguments as ``(end, start, unit)``. Parsed here, each call keeps its arguments
in order and its part is a keyword (``exp.Var``): no pass over columns touches
it, and the call is written back as it was read.

Databricks has two ``DATEDIFF`` forms, which sqlglot parses to one node:
``DATEDIFF(end, start)``, the days between the two dates, and ``DATEDIFF(unit,
start, end)``, the whole units elapsed (its ``TIMESTAMPDIFF``: two hours across
midnight are 0 days). The first stays a two-argument ``DATEDIFF``, the second
becomes ``TIMESTAMPDIFF``, so each keeps its meaning, also written back.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

# The name ``DATEDIFF`` is parsed under, so that the default grammar keeps its
# arguments in order. As long as ``DATEDIFF``, so a position in the parsed text
# addresses the original.
_DATEDIFF_STAND_IN = "DATEDIF_"
# Ossie's calls that take the date part first, and their argument count.
_PART_FIRST = {"DATEADD": 3, "DATEDIFF": 3, "DATE_PART": 2}


def parse_sql(text: str, read: str | None) -> exp.Expr:
    """Parse *text* as sqlglot does, Ossie's date functions as Ossie reads them.

    *read* is the sqlglot dialect; ``None`` is ANSI SQL or Ossie SQL. Snowflake
    parses these calls in its own grammar already; Databricks' ``DATEDIFF`` is
    told apart by its argument count.
    """
    if read not in (None, "databricks"):
        return sqlglot.parse_one(text, read=read)
    tree = sqlglot.parse_one(_stand_in_datediff(text, read), read=read)
    return tree.transform(_part_first if read is None else _databricks_datediff)


def _stand_in_datediff(text: str, read: str | None) -> str:
    try:
        tokens = sqlglot.tokenize(text, read=read)
    except sqlglot.errors.SqlglotError:
        return text  # the parse reports it
    for token, following in zip(tokens, tokens[1:], strict=False):
        if (
            token.token_type is TokenType.VAR
            and token.text.upper() == "DATEDIFF"
            and following.token_type is TokenType.L_PAREN
        ):
            text = text[: token.start] + _DATEDIFF_STAND_IN + text[token.end + 1 :]
    return text


def _part(arg: exp.Expr) -> exp.Var | None:
    """A date part written as a bare word (``day``) or a string (``'day'``)."""
    if isinstance(arg, exp.Column) and not arg.table:
        return exp.var(arg.name.upper())
    if isinstance(arg, exp.Literal) and arg.is_string:
        return exp.var(arg.name.upper())
    return None


def _part_first(node: exp.Expr) -> exp.Expr:
    if not isinstance(node, exp.Anonymous):
        return node
    name = str(node.this).upper()
    stand_in = name == _DATEDIFF_STAND_IN
    if stand_in:
        name = "DATEDIFF"
    # A replaced node's children are not visited, so a call nested in one
    # (``DATEDIFF(day, a, DATEADD(day, 7, b))``) is read here.
    args = [arg.transform(_part_first) for arg in node.expressions]
    part = _part(args[0]) if len(args) == _PART_FIRST.get(name) else None
    if part is not None:
        return exp.Anonymous(this=name, expressions=[part, *args[1:]])
    return exp.Anonymous(this=name, expressions=args) if stand_in else node


def _databricks_datediff(node: exp.Expr) -> exp.Expr:
    if not (isinstance(node, exp.Anonymous) and str(node.this).upper() == _DATEDIFF_STAND_IN):
        return node
    args = [arg.transform(_databricks_datediff) for arg in node.expressions]
    part = _part(args[0]) if len(args) == 3 else None
    if part is not None:
        return exp.TimestampDiff(this=args[2], expression=args[1], unit=part)
    return exp.Anonymous(this="DATEDIFF", expressions=args)

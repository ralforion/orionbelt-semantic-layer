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
converted: arithmetic (``%`` included), ``||``, comparisons, ``IS [NOT]
DISTINCT FROM``, ``AND`` / ``OR`` / ``NOT``, ``IS NULL``, ``IN``, ``BETWEEN``,
``LIKE``, ``ILIKE`` with a string pattern, ``CASE``, literals, and plain function
calls. A SQL ``CAST`` becomes OBML's ``cast(x, 'double')``. Anything else
(``EXTRACT(WEEK FROM ...)``, ``LIKE ... ESCAPE``, a subquery) is reported and
left as it was.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

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


# The clock reads, as OBML's catalog functions.
_CLOCKS: dict[type[exp.Expr], str] = {
    exp.CurrentDate: "current_date",
    exp.CurrentTimestamp: "current_timestamp",
    exp.CurrentTime: "current_time",
}
# Date parts as the unit of OBML's ``date_add``, ``date_diff`` and ``extract``.
_DATE_UNITS = {
    part: part.lower()
    for part in ("YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE", "SECOND")
}
# ``EXTRACT(<part> FROM x)`` parts as the unit of OBML's ``extract(unit, x)``.
# WEEK is OBML's ISO week, as the Ossie spec's weeks start on Monday
# (``DATE_TRUNC('week', d)``) and Snowflake's and Databricks' WEEK are ISO's;
# the ISO names say so outright. DAYOFWEEK is numbered from Sunday on some
# engines and from Monday on others, and the spec does not say which it means,
# so it is left as written; OBML's ``extract`` has no MILLISECOND.
_EXTRACT_UNITS = {
    **_DATE_UNITS,
    "WEEKISO": "week",
    "ISOWEEK": "week",
    "DAYOFWEEKISO": "day_of_week",
    "ISODOW": "day_of_week",
    "DAY_OF_WEEK": "day_of_week",  # OBML's own, as its export writes it
}
_DAY_OF_YEAR = frozenset({"DAYOFYEAR", "DOY"})
# Calls that read one date part, as OBML's ``extract`` unit.
_PART_CALLS: dict[type[exp.Expr], str] = {
    exp.Year: "year",
    exp.Quarter: "quarter",
    exp.Month: "month",
    exp.Day: "day",
    exp.DayOfMonth: "day",
    exp.Hour: "hour",
    exp.Minute: "minute",
    exp.Second: "second",
    exp.DayOfWeekIso: "day_of_week",
}
# A double, as PI() is: a decimal literal times an integer overflowed DuckDB's
# DECIMAL(18) at 3000 * pi.
_PI_DIGITS = exp.Literal.number("3.141592653589793")
_PI = exp.Anonymous(this="cast", expressions=[_PI_DIGITS, exp.Literal.string("double")])


def _call(name: str, *args: exp.Expr) -> exp.Expr:
    return exp.Anonymous(this=name, expressions=list(args))


def _grouped(node: exp.Expr) -> exp.Expr:
    """*node* in parentheses when an operator inside it could bind to a neighbour."""
    return (
        exp.Paren(this=node) if isinstance(node, (exp.Binary, exp.Unary, exp.Predicate)) else node
    )


def _date_unit(part: exp.Expr | None) -> exp.Expr | None:
    """The OBML unit a date part names, as a string literal, or ``None``."""
    unit = _DATE_UNITS.get(part.name.upper()) if isinstance(part, exp.Var) else None
    return exp.Literal.string(unit) if unit else None


def _date(node: exp.Expr) -> exp.Expr:
    """A date argument without the cast to DATE Databricks' ``YEAR(d)`` reads it through."""
    if isinstance(node, exp.TsOrDsToDate) and not node.args.get("format"):
        return node.this
    return node


def _day_of_year(date: exp.Expr) -> exp.Expr:
    """``DAYOFYEAR(d)``, 1 to 366, from OBML's catalog: days since January 1, plus one."""
    # DATE_TRUNC as sqlglot writes one it read, so the Ossie form reads back alike.
    year = exp.DateTrunc(this=date, unit=exp.Literal.string("YEAR"))
    since = _call("date_diff", exp.Literal.string("day"), year, date.copy())
    return exp.Paren(this=exp.Add(this=since, expression=exp.Literal.number(1)))


def whole_unit_counts(tree: exp.Expr) -> list[str]:
    """The unit of each whole-unit count in *tree* that imports as OBML's
    ``date_diff``: Databricks' ``DATEDIFF(unit, start, end)`` and
    ``TIMESTAMPDIFF`` count the units elapsed, ``date_diff`` the boundaries
    crossed, so the imported numbers can differ from the source's."""
    units = (_date_unit(n.args.get("unit")) for n in tree.find_all(exp.TimestampDiff))
    return list(dict.fromkeys(unit.name for unit in units if unit is not None))


def _catalog_form(n: exp.Expr, rewrite: Callable[[exp.Expr], exp.Expr]) -> exp.Expr | None:
    """*n*, a call OBML's catalog has under another name or form, as that
    catalog's call; ``None`` when it has none. Each argument is rewritten here,
    since a replaced node's children are not visited."""

    def arg(node: exp.Expr) -> exp.Expr:
        return node.transform(rewrite)

    if isinstance(n, exp.Extract):
        part = n.name.upper()
        if part in _DAY_OF_YEAR:
            return _day_of_year(arg(_date(n.expression)))
        named = _EXTRACT_UNITS.get(part)
        return _call("extract", exp.Literal.string(named), arg(n.expression)) if named else None
    if isinstance(n, exp.DayOfYear):
        return _day_of_year(arg(_date(n.this)))
    part_unit = _PART_CALLS.get(type(n))
    if part_unit is not None:
        return _call("extract", exp.Literal.string(part_unit), arg(_date(n.this)))
    if isinstance(n, exp.Anonymous):
        return _named_call(str(n.this).upper(), list(n.expressions), arg)
    if isinstance(n, (exp.DateAdd, exp.TimestampAdd)):
        # Snowflake's and Databricks' DATEADD.
        unit = _date_unit(n.args.get("unit"))
        if unit is None or isinstance(n.expression, exp.Interval):
            return None
        return _call("date_add", unit, arg(n.expression), arg(n.this))
    if isinstance(n, exp.DateDiff) and n.args.get("date_part_boundary"):
        # Snowflake's DATEDIFF counts boundaries, as OBML's does.
        unit = _date_unit(n.args.get("unit"))
        return _call("date_diff", unit, arg(n.expression), arg(n.this)) if unit else None
    if isinstance(n, exp.TimestampDiff):
        # Databricks' DATEDIFF(unit, start, end) and TIMESTAMPDIFF count whole
        # units elapsed; imported as OBML's count of boundaries crossed, one
        # meaning on every engine, with a warning (see whole_unit_counts).
        unit = _date_unit(n.args.get("unit"))
        return _call("date_diff", unit, arg(n.expression), arg(n.this)) if unit else None
    if isinstance(n, exp.Trim) and _trims_spaces(n):
        position = str(n.args.get("position") or "").upper()
        name = {"LEADING": "ltrim", "TRAILING": "rtrim"}.get(position, "trim")
        return _call(name, arg(n.this))
    if isinstance(n, exp.Nvl2):
        otherwise = n.args.get("false")
        return exp.Case(
            ifs=[
                exp.If(
                    this=exp.Not(this=exp.Is(this=arg(n.this), expression=exp.Null())),
                    true=arg(n.args["true"]),
                )
            ],
            default=arg(otherwise) if otherwise is not None else exp.Null(),
        )
    if (
        isinstance(n, exp.StrPosition)
        and not n.args.get("position")
        and not n.args.get("occurrence")
    ):
        return _call("position", arg(n.args["substr"]), arg(n.this))
    if isinstance(n, exp.Contains) and len(n.args) == 2:
        found = _call("position", arg(n.expression), arg(n.this))
        return exp.Paren(this=exp.GT(this=found, expression=exp.Literal.number(0)))
    if isinstance(n, exp.Left):
        return _call("substring", arg(n.this), exp.Literal.number(1), arg(n.expression))
    if isinstance(n, exp.Right):
        # The last k characters start at length - k + 1; all of a shorter string.
        start = exp.Add(
            this=exp.Sub(this=_call("length", arg(n.this)), expression=_grouped(arg(n.expression))),
            expression=exp.Literal.number(1),
        )
        return _call("substring", arg(n.this), _call("greatest", start, exp.Literal.number(1)))
    if isinstance(n, exp.Pi):
        return _PI.copy()
    if isinstance(n, (exp.Degrees, exp.Radians)):
        # In double, as the engines' own DEGREES and RADIANS are: an integer
        # times 180 overflows, and one times a decimal pi did at 3000.
        scale = (exp.Literal.number(180), _PI_DIGITS.copy())
        num, den = scale if isinstance(n, exp.Degrees) else scale[::-1]
        double = _call("cast", arg(n.this), exp.Literal.string("double"))
        return exp.Paren(this=exp.Div(this=exp.Mul(this=double, expression=num), expression=den))
    return None


def _trims_spaces(n: exp.Trim) -> bool:
    """Whether a ``TRIM`` strips spaces, as OBML's ``trim`` does: none named, or ``' '``."""
    chars = n.expression
    if chars is None:
        return True
    return isinstance(chars, exp.Literal) and chars.is_string and chars.this == " "


def _named_call(
    name: str, args: list[exp.Expr], arg: Callable[[exp.Expr], exp.Expr]
) -> exp.Expr | None:
    """A call sqlglot's default grammar does not know, as OBML's catalog has it."""
    if name == "DATEDIFF" and len(args) == 2:
        # DATEDIFF(end, start), Databricks' and MySQL's: the days between the
        # two dates, which are the day boundaries crossed.
        return _call("date_diff", exp.Literal.string("day"), arg(args[1]), arg(args[0]))
    if name in ("DATEADD", "DATEDIFF") and len(args) == 3:
        # Ossie's own (``_ossie_parse``): the part first, then the amount and
        # the date, or the start and the end. Ossie's DATEDIFF counts
        # boundaries, as Tableau's and DAX's, which its spec maps it to, do.
        unit = _date_unit(args[0])
        target = "date_add" if name == "DATEADD" else "date_diff"
        return _call(target, unit, arg(args[1]), arg(args[2])) if unit else None
    if name == "DATE_PART" and len(args) == 2 and isinstance(args[0], exp.Var):
        part = args[0].name.upper()
        if part in _DAY_OF_YEAR:
            return _day_of_year(arg(args[1]))
        named = _EXTRACT_UNITS.get(part)
        return _call("extract", exp.Literal.string(named), arg(args[1])) if named else None
    if name == "IFF" and len(args) == 3:
        return exp.Case(ifs=[exp.If(this=arg(args[0]), true=arg(args[1]))], default=arg(args[2]))
    if name == "ZEROIFNULL" and len(args) == 1:
        return _call("coalesce", arg(args[0]), exp.Literal.number(0))
    if name == "NULLIFZERO" and len(args) == 1:
        return _call("nullif", arg(args[0]), exp.Literal.number(0))
    return None


def render_obml(node: exp.Expr, column: Callable[[exp.Column], exp.Expr]) -> str:
    """Render *node* as OBML text, each column written by *column* (see :func:`obml_tree`)."""
    return obml_tree(node, column).sql()


def obml_tree(node: exp.Expr, column: Callable[[exp.Column], exp.Expr]) -> exp.Expr:
    """*node* rewritten into the forms OBML reads, each column written by *column*.

    A SQL ``CAST(x AS DOUBLE)`` becomes OBML's ``cast(x, 'double')``, the only
    cast form OBML parses; a target it has no type for raises
    :class:`NotRepresentableError`. ``CURRENT_DATE``, ``CURRENT_TIMESTAMP`` and
    ``CURRENT_TIME`` become OBML's ``current_date()``, ``current_timestamp()``
    and ``current_time()``, the session's clock as the source's is, unless the
    model sets a ``queryTimezone``; bare, OBML reads each word as a string. One
    with an argument (a precision, a zone) raises: OBML's take none.

    A call OBML's catalog has under another name or form becomes that call
    (:func:`_catalog_form`): ``EXTRACT(YEAR FROM x)``, ``YEAR(x)`` and
    ``DATE_PART('year', x)`` OBML's ``extract('year', x)``, ``DATEADD`` and
    ``DATEDIFF`` its ``date_add`` and ``date_diff``, ``TRIM(BOTH ' ' FROM s)``
    its ``trim(s)``, and so on. Any other is left as written, which
    :func:`obml_unreadable_text` refuses where OBML does.
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
        clock = _CLOCKS.get(type(n))
        if clock is not None:
            if any(n.args.values()):
                raise NotRepresentableError(f"{n.sql()}: OBML's {clock}() takes no argument")
            return exp.Anonymous(this=clock, expressions=[])
        catalog = _catalog_form(n, rewrite)
        return n if catalog is None else catalog

    return node.transform(rewrite)


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
    exp.Mod,
    exp.DPipe,
    exp.Neg,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.NullSafeEQ,
    exp.NullSafeNEQ,
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
    if isinstance(node, exp.ILike):
        # OBML's ILIKE takes a string literal pattern only, where a backslash
        # escapes. Snowflake and ANSI SQL have no default escape character, and
        # string literals escape differently from engine to engine, so a pattern
        # with a backslash is kept aside rather than matched differently.
        pattern = node.expression
        return isinstance(pattern, exp.Literal) and pattern.is_string and "\\" not in pattern.this
    return isinstance(node, _OPERATORS)


def _describe(node: exp.Expr) -> str:
    return f"{type(node).__name__.upper()} ({node.sql()})"


def unreadable(tree: exp.Expr) -> str | None:
    """Why OBML cannot read *tree* as a computed column, or ``None`` when it can.

    *tree* is as :func:`obml_tree` rewrote it, so a call OBML's catalog has
    under another name or form (``YEAR(d)``) counts as that call.
    """
    for node in tree.walk():
        if not (isinstance(node, _FUNCTIONS) or _in_grammar(node)):
            return _describe(node)
    return None


def unreadable_operator(tree: exp.Expr) -> str | None:
    """The first operator or predicate in *tree* OBML's expression grammar lacks.

    A measure expression OBML cannot parse stops the whole model loading
    (``INVALID_MEASURE_EXPRESSION``). Its grammar calls any function written
    ``NAME(args)``, so only operators and predicates are checked: bitwise
    operators, ``ILIKE`` against a column and ``LIKE ... ESCAPE`` are not in it.
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
# ``{[Measure]}`` in a metric formula.
MEASURE_REF = re.compile(r"\{\[[^\]]+\]\}")
SQL_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
# A name OBML resolves as itself: it strips the name and writes it into
# ``{[Data Object].[name]}``, which ends at the first ``]``; a bare number is
# read as a regex quantifier.
_SIBLING_NAME = re.compile(r"\w[^}\]]*")
_REGEX_QUANTIFIER = re.compile(r"\d+(?:,\d*)?")
# What OBML's tokenizer reads besides literals and references.
_OBML_CHARACTERS = re.compile(r"[\w\s+\-*/%(),.=<>!]")
# A number OBML reads as two tokens: 1e2 is "1" then "e2".
_NUMBER_THEN_WORD = re.compile(r"(?<![\w.])\d[\d.]*[A-Za-z_]")
# A word, and the words OBML's grammar reads other than a function's name: its
# keywords and literals. Any other word is SQL syntax inside a call
# (``EXTRACT(DAYOFWEEK FROM x)``, ``TRIM(BOTH ' ' FROM s)``) or a bare keyword
# (``INTERVAL '1' DAY``, ``CURRENT_DATE``), which OBML refuses or reads as text.
_WORD = re.compile(r"(?<![\w.])[A-Za-z_]\w*")
_OBML_WORDS = frozenset(
    {"AND", "OR", "NOT", "CASE", "WHEN", "THEN", "ELSE", "END", "IS", "IN", "BETWEEN"}
    | {"LIKE", "ILIKE", "DISTINCT", "FROM", "NULL", "TRUE", "FALSE", "DATE", "TIMESTAMP"}
    | {"TIME"}
)
# An expression's tokens as OBML's call scanner sees them: a string literal or a
# reference whole, a word, or one character.
_TOKEN = re.compile(r"'(?:[^']|'')*'|\{[^}]*\}|[A-Za-z_]\w*|\S")
_FUNCTION_TABLE = Path(__file__).resolve().parent / "schemas" / "obml-functions.json"


@cache
def _obml_functions() -> dict[str, dict[str, Any]]:
    """OBML's catalog functions: arity, and for a date/time function its units.

    Written from OrionBelt's catalog by ``scripts/refresh_osi_function_table.py``
    and kept in sync by a drift-guard test, as the OBML schema is.
    """
    table: dict[str, dict[str, Any]] = json.loads(_FUNCTION_TABLE.read_text())
    return table


def _calls(expression: str) -> list[tuple[str, list[str]]]:
    """Each function call in *expression*, with the text of each argument."""
    tokens = _TOKEN.findall(expression)
    calls: list[tuple[str, list[str]]] = []
    frames: list[tuple[str | None, list[list[str]]]] = []
    for index, token in enumerate(tokens):
        if token == "(":
            before = tokens[index - 1] if index else ""
            called = _WORD.fullmatch(before) and before.upper() not in _OBML_WORDS
            frames.append((before if called else None, [[]]))
            continue
        if token == ")" and frames:
            name, arguments = frames.pop()
            if name is not None:
                texts = [" ".join(argument) for argument in arguments]
                calls.append((name, texts if any(texts) else []))
        elif token == "," and frames:
            frames[-1][1].append([])
            continue
        if frames:
            frames[-1][1][-1].append(token)
    return calls


def _unit(argument: str) -> str | None:
    """The unit a quoted literal names, lower case, as OBML reads it; else ``None``."""
    quoted = len(argument) >= 2 and argument.startswith("'") and argument.endswith("'")
    return argument[1:-1].lower() if quoted else None


def _catalog_misuse(expression: str) -> str | None:
    """A call OBML's catalog refuses: a wrong argument count, or an unknown unit."""
    for name, arguments in _calls(expression):
        spec = _obml_functions().get(name.lower())
        if spec is None:
            continue
        low, high = spec["args"]
        if len(arguments) < low or (high is not None and len(arguments) > high):
            return f"{name} with {len(arguments)} arguments"
        unit = spec.get("unit")
        if unit is not None and _unit(arguments[unit]) not in spec["units"]:
            return f"{name} with unit {arguments[unit]}"
    return None


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

    The last check before a computed column replaces a field's code, or a
    measure expression or metric formula is written: a model that loaded must
    not stop loading because of an expression no query reads.
    """
    text = SQL_STRING_LITERAL.sub("''", expression)
    text = QUALIFIED_REF.sub("0", text)
    text = MEASURE_REF.sub("0", text)
    text = SIBLING_REF.sub("0", text)
    if _NUMBER_THEN_WORD.search(text):
        return "a number OBML reads as two tokens"
    for word in _WORD.finditer(text):
        call = text[word.end() :].lstrip().startswith("(")
        if not call and word.group(0).upper() not in _OBML_WORDS:
            return f"a word OBML does not read: {word.group(0)}"
    misuse = _catalog_misuse(expression)
    if misuse:
        return f"a call OBML's function catalog refuses: {misuse}"
    # ``||`` is concatenation; a single ``|`` is not OBML.
    stray = sorted(set(_OBML_CHARACTERS.sub("", text.replace("''", "").replace("||", ""))))
    if stray:
        return f"characters OBML does not read: {' '.join(stray)}"
    return None

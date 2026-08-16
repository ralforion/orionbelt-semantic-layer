"""Placeholder scanning for computed-column and measure expressions.

A computed column's ``expression`` refers to sibling columns of the same data
object with single-brace ``{Column}`` placeholders, and to a column of another
data object with the qualified ``{[Data Object].[Column]}`` form measure
expressions use. Three call sites need to agree on exactly which braces count
as a placeholder:

* ``parser/validator.py`` reports a placeholder that names no sibling column.
* ``compiler/resolution.py`` rewrites placeholders to the qualified
  ``{[Object].[Column]}`` form before tokenizing.
* ``compiler/expr_parser.py`` does the same when inlining a nested computed
  column.

They used to carry three private copies of the pattern, kept in sync by
comment. The rule now lives here once, so the validator cannot accept a
placeholder the compiler would resolve differently.

``models`` is the shared layer: ``parser`` must not import from ``compiler``,
and both already depend on ``models``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

COMPUTED_PLACEHOLDER = re.compile(r"\{(\w[^}]*)\}")
"""``{ColumnName}`` placeholder inside a computed-column expression body.

Requires a word character after the brace, so the qualified
:data:`QUALIFIED_COLUMN_REF` form passes through untouched.
"""

QUALIFIED_COLUMN_REF = re.compile(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}")
"""``{[Data Object].[Column]}`` — a column named together with its object."""

SQL_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
"""A single-quoted SQL string literal, ``''`` being the escaped quote."""

_REGEX_QUANTIFIER = re.compile(r"^\d+(?:,\d*)?$")
"""``{4}``, ``{2,}``, ``{2,4}`` - a regex quantifier, never a column name."""


def _segments(expression: str) -> Iterator[tuple[str, bool]]:
    """Split *expression* into ``(text, is_string_literal)`` pairs, in order."""
    pos = 0
    for match in SQL_STRING_LITERAL.finditer(expression):
        if match.start() > pos:
            yield expression[pos : match.start()], False
        yield match.group(0), True
        pos = match.end()
    if pos < len(expression):
        yield expression[pos:], False


def substitute_placeholders(expression: str, repl: Callable[[re.Match[str]], str]) -> str:
    """Apply *repl* to every placeholder outside a string literal.

    Braces inside a literal are data, not references: an expression such as
    ``regexp_extract({Zip}, '[0-9]{5}')`` must keep its quantifier, and
    ``'{Amount}'`` must stay the four characters the author typed rather than
    becoming a column reference embedded in a quoted string.
    """
    return "".join(
        text if is_literal else COMPUTED_PLACEHOLDER.sub(repl, text)
        for text, is_literal in _segments(expression)
    )


def find_placeholders(expression: str) -> list[str]:
    """The column names *expression* references, in order of appearance.

    Skips string literals for the reason above, and skips a bare regex
    quantifier even outside one - a dialect that quotes patterns with double
    quotes puts ``{5}`` beyond the reach of :data:`SQL_STRING_LITERAL`, and
    reporting a missing column named ``5`` would be worse than staying quiet.
    """
    names: list[str] = []
    for text, is_literal in _segments(expression):
        if is_literal:
            continue
        for raw in COMPUTED_PLACEHOLDER.findall(text):
            name = raw.strip()
            if not _REGEX_QUANTIFIER.match(name):
                names.append(name)
    return names


@dataclass(frozen=True)
class FunctionCallRef:
    """A ``name(...)`` call found in an expression body, with its arguments.

    ``arguments`` holds each argument's source text, stripped: the validator
    needs it for the date/time entries, whose first argument must be a literal
    time unit the renderers can switch on. Everything else only reads the
    count.
    """

    name: str
    arguments: tuple[str, ...] = ()

    @property
    def arg_count(self) -> int:
        return len(self.arguments)


BOOLEAN_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})
"""Logical operators, spelled as words."""

SQL_KEYWORDS: frozenset[str] = frozenset(
    {"CASE", "WHEN", "THEN", "ELSE", "END", "IS", "IN", "BETWEEN", "LIKE"}
)
"""Predicate and control-flow keywords the expression grammar recognises.

Kept here rather than in ``compiler/expr_parser.py`` for the reason the whole
module exists: the tokenizer and the function-call scanner have to agree on
what a keyword is. While the scanner held its own shorter copy,
``CASE WHEN {A} > 1 THEN (2 + 3) ELSE (4) END`` was read as calls to ``THEN``
and ``ELSE`` — invisible while only catalog names are checked, and a rejected
model as soon as a mode rejects names the catalog does not carry.
"""

_KEYWORDS_BEFORE_PAREN: frozenset[str] = BOOLEAN_KEYWORDS | SQL_KEYWORDS
"""Words that can precede ``(`` without being a function call.

``x IN (1, 2)`` would otherwise read as a two-argument call to ``IN``, and the
catalog check would answer for a function nobody wrote.
"""

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


@dataclass
class _OpenParen:
    """An open parenthesis during the scan — a call when it carries a name."""

    name: str | None
    start: int
    commas: list[int] = field(default_factory=list)
    has_content: bool = False


def find_function_calls(expression: str) -> list[FunctionCallRef]:
    """Every function call *expression* makes, with the number of arguments.

    Ordered by closing parenthesis, so a nested call is reported before the one
    that contains it. A scan rather than a parse: ``parser/validator.py`` must
    not import from ``compiler``, where the real expression parser lives, and
    the check this feeds — name and arity against the portable function catalog
    — needs no parse tree, only the call shapes. What the two must agree on is
    :data:`SQL_KEYWORDS`, which they now share.

    Braces and string literals are skipped whole, so a delimiter inside a
    literal (``split_part({Path}, ',', 2)``) is not counted as an argument
    separator and a column name containing a comma is not counted at all.
    """
    calls: list[FunctionCallRef] = []
    stack: list[_OpenParen] = []
    pos = 0
    length = len(expression)

    def mark_content() -> None:
        if stack:
            stack[-1].has_content = True

    def split_arguments(frame: _OpenParen, close: int) -> tuple[str, ...]:
        """The source text of each argument, from the recorded comma offsets."""
        if not frame.has_content:
            return ()
        bounds = [frame.start, *frame.commas, close]
        return tuple(
            expression[bounds[i] + 1 : bounds[i + 1]].strip() for i in range(len(bounds) - 1)
        )

    while pos < length:
        char = expression[pos]
        if char.isspace():
            pos += 1
            continue
        if char == "'":
            literal = SQL_STRING_LITERAL.match(expression, pos)
            mark_content()
            # An unterminated literal swallows the rest — the parser reports it.
            pos = literal.end() if literal else length
            continue
        if char == "{":
            end = expression.find("}", pos)
            mark_content()
            pos = length if end == -1 else end + 1
            continue
        identifier = _IDENTIFIER.match(expression, pos)
        if identifier:
            after = pos + len(identifier.group(0))
            while after < length and expression[after].isspace():
                after += 1
            mark_content()
            if after < length and expression[after] == "(":
                name = identifier.group(0)
                is_call = name.upper() not in _KEYWORDS_BEFORE_PAREN
                stack.append(_OpenParen(name=name if is_call else None, start=after))
                pos = after + 1
                continue
            pos = after
            continue
        if char == "(":
            mark_content()
            stack.append(_OpenParen(name=None, start=pos))
            pos += 1
            continue
        if char == ")":
            if stack:
                frame = stack.pop()
                if frame.name is not None:
                    calls.append(
                        FunctionCallRef(name=frame.name, arguments=split_arguments(frame, pos))
                    )
            mark_content()
            pos += 1
            continue
        if char == ",":
            if stack:
                stack[-1].commas.append(pos)
                stack[-1].has_content = True
            pos += 1
            continue
        mark_content()
        pos += 1
    return calls


def find_qualified_refs(expression: str) -> list[tuple[str, str]]:
    """The ``(data object, column)`` pairs *expression* names, in order.

    The qualified counterpart of :func:`find_placeholders`: a computed column
    reads a *sibling* through ``{Column}`` and a column of another data object
    through ``{[Data Object].[Column]}``. Skips string literals for the same
    reason.
    """
    refs: list[tuple[str, str]] = []
    for text, is_literal in _segments(expression):
        if is_literal:
            continue
        refs.extend((obj.strip(), col.strip()) for obj, col in QUALIFIED_COLUMN_REF.findall(text))
    return refs

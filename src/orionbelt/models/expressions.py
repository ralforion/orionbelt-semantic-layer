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

"""Splitting a simple-query message into its statements.

The Postgres simple-query protocol lets one ``Query`` message carry several
statements separated by semicolons. The server runs them in order and replies
with **one result set per statement**, then a single ``ReadyForQuery``. A
client that sends three statements and receives one reply does not report a
protocol error - it reads a result that is not there. DuckDB's ``postgres``
extension enumerates a catalog with exactly such a batch and dies with an
internal error naming a vector index, which says nothing about what went
wrong.

Splitting on ``;`` is the obvious implementation and the wrong one: a
semicolon inside a string, an identifier, a comment or a dollar-quoted body
is data, not a boundary. So this is a scanner rather than a ``split``.

What it understands, all of which appear in real client traffic:

* ``'...'`` string literals, with ``''`` as the escaped quote
* ``E'...'`` escape strings, where a backslash escapes the next character
* ``"..."`` quoted identifiers, with ``""`` as the escaped quote
* ``$$...$$`` and ``$tag$...$tag$`` dollar quoting
* ``--`` line comments
* ``/* ... */`` block comments, **nested**, which Postgres allows and most
  splitters get wrong

A fragment that is only comments and whitespace is dropped rather than
returned. ``SELECT 1; -- done`` is one statement with a trailing remark, not
two, and dispatching the remark as a second statement made the client read a
successful result followed by an error. Comments *attached* to a statement stay
attached: stripping them is the parser's business, not the splitter's.
"""

from __future__ import annotations

import re

#: A dollar-quote opener: ``$$`` or ``$tag$``. The tag rules follow an
#: identifier, and the closing delimiter must repeat it exactly.
_DOLLAR_OPEN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_statements(sql: str) -> list[str]:
    """Split *sql* into statements, ignoring empty ones.

    A single statement with no trailing semicolon comes back unchanged, so
    the common case costs one scan and allocates one list.
    """
    statements: list[str] = []
    start = 0
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        if ch == "'":
            i = _skip_quoted(sql, i, "'")
            continue
        if ch == '"':
            i = _skip_quoted(sql, i, '"')
            continue
        if ch in "Ee" and i + 1 < n and sql[i + 1] == "'":
            # E'...' - a backslash escapes the next character, unlike a
            # plain literal where only '' does.
            i = _skip_escape_string(sql, i + 1)
            continue
        if ch == "$":
            end = _skip_dollar_quoted(sql, i)
            if end != i:
                i = end
                continue
        if ch == "-" and sql.startswith("--", i):
            newline = sql.find("\n", i)
            i = n if newline == -1 else newline + 1
            continue
        if ch == "/" and sql.startswith("/*", i):
            i = _skip_block_comment(sql, i)
            continue

        if ch == ";":
            _append(statements, sql[start:i])
            start = i + 1

        i += 1

    _append(statements, sql[start:])
    return statements


def _append(statements: list[str], fragment: str) -> None:
    """Add *fragment* unless it is empty or holds nothing but comments."""
    fragment = fragment.strip()
    if fragment and _has_code(fragment):
        statements.append(fragment)


def _has_code(fragment: str) -> bool:
    """Whether *fragment* contains anything outside comments and whitespace.

    The scan mirrors :func:`split_statements` for the two comment forms and
    stops at the first character that is neither - it does not need to
    understand quoting, because a quote *is* such a character.
    """
    i = 0
    n = len(fragment)
    while i < n:
        ch = fragment[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "-" and fragment.startswith("--", i):
            newline = fragment.find("\n", i)
            i = n if newline == -1 else newline + 1
            continue
        if ch == "/" and fragment.startswith("/*", i):
            i = _skip_block_comment(fragment, i)
            continue
        return True
    return False


def _skip_quoted(sql: str, i: int, quote: str) -> int:
    """Index just past a ``'...'`` or ``"..."`` run, doubling as the escape."""
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return n  # unterminated: the caller sees the rest as one statement


def _skip_escape_string(sql: str, i: int) -> int:
    """Index just past an ``E'...'`` body, where ``\\`` escapes anything."""
    n = len(sql)
    i += 1
    while i < n:
        if sql[i] == "\\":
            i += 2
            continue
        if sql[i] == "'":
            if i + 1 < n and sql[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return n


def _skip_dollar_quoted(sql: str, i: int) -> int:
    """Index just past a dollar-quoted body, or *i* when this is not one.

    ``$1`` is a parameter placeholder rather than a quote opener, which is
    why a non-match returns the position unchanged instead of guessing.
    """
    match = _DOLLAR_OPEN.match(sql, i)
    if match is None:
        return i
    delimiter = match.group(0)
    close = sql.find(delimiter, match.end())
    if close == -1:
        return len(sql)
    return close + len(delimiter)


def _skip_block_comment(sql: str, i: int) -> int:
    """Index just past a ``/* ... */`` comment. Postgres nests these."""
    n = len(sql)
    depth = 0
    while i < n:
        if sql.startswith("/*", i):
            depth += 1
            i += 2
            continue
        if sql.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return n

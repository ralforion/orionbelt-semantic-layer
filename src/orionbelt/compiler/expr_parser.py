"""Generic expression tokenizer and recursive descent parser.

Handles two expression syntaxes:
- Metric formulas: ``{[Measure Name]}`` references → ``ColumnRef(name=...)``
- Measure expressions: ``{[DataObject].[Column]}`` references → ``ColumnRef(name=..., table=...)``

Both share the same grammar:

    expr   → or_expr
    or_expr   → and_expr ('OR' and_expr)*
    and_expr  → not_expr ('AND' not_expr)*
    not_expr  → 'NOT' not_expr | cmp_expr
    cmp_expr  → add_expr (('=' | '<>' | '!=' | '<' | '<=' | '>' | '>=') add_expr)?
    add_expr  → mul_expr (('+' | '-') mul_expr)*
    mul_expr  → factor (('*' | '/') factor)*
    factor → '(' expr ')'
           | NUMBER
           | STRING
           | REF
           | IDENT '(' arg_list? ')'  -- function call
           | IDENT                     -- bare keyword (TRUE/FALSE/NULL)
    arg_list → expr (',' expr)*
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import BinaryOp, ColumnRef, Expr, FunctionCall, Literal, UnaryOp

if TYPE_CHECKING:
    from orionbelt.models.semantic import SemanticModel


@dataclass
class _Token:
    """A token from expression tokenization."""

    # Kinds:
    #   "ref" — metric-formula ``{[Name]}`` reference (unqualified)
    #   "colref" — measure-expression ``{[Obj].[Col]}`` reference (qualified)
    #   "number" — numeric literal
    #   "string" — string literal ('...')
    #   "ident" — bare identifier (function name or keyword)
    #   "op" — operator (+ - * / = <> != < <= > >=) plus AND / OR / NOT
    #   "lparen" / "rparen" — grouping
    #   "comma" — argument separator
    kind: str
    value: str


# Comparison operators ordered longest-first so the tokenizer prefers
# ``<=`` and ``>=`` over ``<`` / ``>``. ``!=`` is accepted as an alias
# for ``<>``.
_COMPARISON_OPS: tuple[str, ...] = ("<=", ">=", "<>", "!=", "=", "<", ">")

# Reserved keyword tokens emitted as ``op`` with the uppercased name so
# the parser can treat them uniformly with the symbolic operators.
_BOOLEAN_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT"})

# Bare-identifier literals — emitted as their typed ``Literal`` node by
# the parser. Keep uppercase so case-insensitive matching is one lookup.
_LITERAL_KEYWORDS: dict[str, str | int | float | bool | None] = {
    "TRUE": True,
    "FALSE": False,
    "NULL": None,
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")

# ``{ColumnName}`` placeholder inside a computed-column expression body.
# Same shape as ``compiler.resolution._COMPUTED_PLACEHOLDER`` — kept here
# to avoid a circular import; both must match the OBML spec rule
# "computed-column expressions use ``{column}`` for sibling columns
# and ``{[obj].[col]}`` for cross-object references".
_COMPUTED_PLACEHOLDER = re.compile(r"\{(\w[^}]*)\}")


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

_MEASURE_REF_PATTERN = re.compile(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}", re.DOTALL)


def _tokenize_common(formula: str, tokens: list[_Token], start: int) -> int:
    """Tokenize a common token at position *start*.

    Returns the new position after the consumed token, or *start + 1* if
    the character can't be classified (it's skipped). Recognised:
    numbers, strings, identifiers (function names + boolean keywords +
    bare literals like ``TRUE``/``FALSE``/``NULL``), arithmetic
    operators, comparison operators (``= <> != < <= > >=``), parens,
    commas, and whitespace.
    """
    ch = formula[start]
    if ch in " \t\n":
        return start + 1
    if ch in "0123456789" or (
        ch == "." and start + 1 < len(formula) and formula[start + 1].isdigit()
    ):
        j = start
        while j < len(formula) and (formula[j].isdigit() or formula[j] == "."):
            j += 1
        tokens.append(_Token(kind="number", value=formula[start:j]))
        return j
    if ch == "'":
        # Single-quoted string literal — support ``''`` as an escaped quote.
        j = start + 1
        buf: list[str] = []
        while j < len(formula):
            if formula[j] == "'":
                if j + 1 < len(formula) and formula[j + 1] == "'":
                    buf.append("'")
                    j += 2
                    continue
                tokens.append(_Token(kind="string", value="".join(buf)))
                return j + 1
            buf.append(formula[j])
            j += 1
        # Unclosed string — emit what we have so the parser surfaces a
        # later error rather than crashing the tokenizer.
        tokens.append(_Token(kind="string", value="".join(buf)))
        return j
    # Comparison operators (longest first).
    for sym in _COMPARISON_OPS:
        if formula.startswith(sym, start):
            tokens.append(_Token(kind="op", value=sym))
            return start + len(sym)
    if ch in "+-*/":
        tokens.append(_Token(kind="op", value=ch))
        return start + 1
    if ch == "(":
        tokens.append(_Token(kind="lparen", value="("))
        return start + 1
    if ch == ")":
        tokens.append(_Token(kind="rparen", value=")"))
        return start + 1
    if ch == ",":
        tokens.append(_Token(kind="comma", value=","))
        return start + 1
    # Bare identifier — function name, boolean keyword, or literal
    # (TRUE / FALSE / NULL). Match greedily on the standard word
    # character class.
    m = _IDENT_RE.match(formula, start)
    if m:
        ident = m.group(0)
        upper = ident.upper()
        if upper in _BOOLEAN_KEYWORDS:
            tokens.append(_Token(kind="op", value=upper))
        else:
            tokens.append(_Token(kind="ident", value=ident))
        return m.end()
    return start + 1  # skip unrecognised


def tokenize_metric_formula(formula: str) -> list[_Token]:
    """Tokenize a metric formula with ``{[Measure Name]}`` references."""
    tokens: list[_Token] = []
    i = 0
    while i < len(formula):
        ch = formula[i]
        if ch == "{" and i + 1 < len(formula) and formula[i + 1] == "[":
            end = formula.find("]}", i + 2)
            if end == -1:
                raise ValueError("Unclosed {[...]} reference in metric formula")
            ref_name = formula[i + 2 : end]
            if "{[" in ref_name:
                raise ValueError("Unclosed {[...]} reference in metric formula")
            tokens.append(_Token(kind="ref", value=ref_name))
            i = end + 2
        else:
            i = _tokenize_common(formula, tokens, i)
    return tokens


def tokenize_measure_expression(
    formula: str,
    model: SemanticModel,
    _seen: frozenset[tuple[str, str]] = frozenset(),
) -> list[_Token]:
    """Tokenize a measure expression with ``{[DataObject].[Column]}`` references.

    Column references resolve as follows:

    * Base column (``code:`` present) — emit a single ``"colref"`` token
      carrying ``table\\0source_code`` in its value.
    * Computed column (``expression:`` set) — recursively tokenize the
      referenced column's expression body in-place, wrapped in
      ``( ... )`` so it composes correctly with surrounding operators.
      Cycle detection via the ``_seen`` set raises
      ``RecursionError`` if a chain of expression columns loops.
    * Unknown column / data object — emit a ``colref`` carrying the
      label as the source name (downstream renderer surfaces an
      empty-identifier error or treats the label as the column code,
      matching pre-v2.6.1 behaviour for that path).
    """
    tokens: list[_Token] = []
    i = 0
    while i < len(formula):
        ch = formula[i]
        if ch == "{" and i + 1 < len(formula) and formula[i + 1] == "[":
            m = _MEASURE_REF_PATTERN.match(formula, i)
            if m:
                obj_name, col_name = m.group(1), m.group(2)
                obj = model.data_objects.get(obj_name)
                column = obj.columns.get(col_name) if obj else None
                if column is not None and column.expression:
                    key = (obj_name, col_name)
                    if key in _seen:
                        raise RecursionError(
                            f"Cyclic computed-column reference: {obj_name}.{col_name}"
                        )
                    # Rewrite ``{name}`` shorthand to ``{[obj].[name]}``
                    # the same way :func:`_build_computed_column_expr`
                    # does, so the nested tokenizer sees fully-qualified
                    # placeholders and resolves them against this model.
                    inner = column.expression or ""

                    def _sub(
                        match: re.Match[str],
                        _obj: object = obj,
                        _default_label: str = obj_name,
                    ) -> str:
                        name = match.group(1).strip()
                        cols = getattr(_obj, "columns", None) if _obj is not None else None
                        if cols is not None and name in cols:
                            label = getattr(_obj, "label", _default_label) or _default_label
                            return f"{{[{label}].[{name}]}}"
                        return match.group(0)

                    rewritten = _COMPUTED_PLACEHOLDER.sub(_sub, inner)
                    inner_tokens = tokenize_measure_expression(
                        rewritten, model, _seen=_seen | {key}
                    )
                    # Wrap the inlined token stream in parentheses so it
                    # binds as a single factor in the outer expression.
                    tokens.append(_Token(kind="lparen", value="("))
                    tokens.extend(inner_tokens)
                    tokens.append(_Token(kind="rparen", value=")"))
                else:
                    source = column.code if column is not None and column.code else col_name
                    tokens.append(_Token(kind="colref", value=f"{obj_name}\0{source}"))
                i = m.end()
            else:
                i += 1
        else:
            i = _tokenize_common(formula, tokens, i)
    return tokens


# ---------------------------------------------------------------------------
# Parsing (recursive descent, shared by both expression types)
# ---------------------------------------------------------------------------


def parse_expression(tokens: list[_Token]) -> Expr:
    """Parse tokens into an AST with correct operator precedence.

    Handles ``"ref"`` tokens (metric formula → unqualified ColumnRef) and
    ``"colref"`` tokens (measure expression → qualified ColumnRef)
    uniformly, plus arithmetic, comparison, logical, and function-call
    surface needed by computed-column expressions.
    """
    pos = [0]

    def _peek() -> _Token | None:
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def _advance() -> _Token:
        tok = tokens[pos[0]]
        pos[0] += 1
        return tok

    def _is_op(value: str) -> bool:
        t = _peek()
        return t is not None and t.kind == "op" and t.value == value

    def _parse_arg_list() -> list[Expr]:
        """Parse a (possibly empty) comma-separated list of expressions
        up to the matching ``)``. Caller has already consumed the ``(``.
        """
        args: list[Expr] = []
        if _peek() and _peek().kind == "rparen":  # type: ignore[union-attr]
            _advance()
            return args
        args.append(_parse_or())
        while _peek() and _peek().kind == "comma":  # type: ignore[union-attr]
            _advance()
            args.append(_parse_or())
        if _peek() and _peek().kind == "rparen":  # type: ignore[union-attr]
            _advance()
        return args

    def _parse_factor() -> Expr:
        tok = _peek()
        if tok is None:
            return Literal.number(0)
        if tok.kind == "lparen":
            _advance()
            node = _parse_or()
            if _peek() and _peek().kind == "rparen":  # type: ignore[union-attr]
                _advance()
            return node
        if tok.kind == "number":
            _advance()
            val = float(tok.value) if "." in tok.value else int(tok.value)
            return Literal.number(val)
        if tok.kind == "string":
            _advance()
            return Literal.string(tok.value)
        if tok.kind == "ident":
            _advance()
            upper = tok.value.upper()
            # Bare keyword literal — TRUE / FALSE / NULL.
            if upper in _LITERAL_KEYWORDS:
                lit_val = _LITERAL_KEYWORDS[upper]
                return Literal(value=lit_val)
            # Function call — IDENT must be followed by ``(``.
            if _peek() and _peek().kind == "lparen":  # type: ignore[union-attr]
                _advance()  # consume '('
                args = _parse_arg_list()
                return FunctionCall(name=tok.value, args=args)
            # Bare identifier without a call — surface as a literal so
            # the SQL emitter renders it verbatim. Used for SQL keyword
            # operands we don't otherwise model (e.g. ``CURRENT_DATE``
            # in some dialects).
            return Literal.string(tok.value)
        if tok.kind == "ref":
            _advance()
            return ColumnRef(name=tok.value)
        if tok.kind == "colref":
            _advance()
            table, column = tok.value.split("\0", 1)
            return ColumnRef(name=column, table=table)
        # Unknown leading token — skip to keep the parser robust.
        _advance()
        return Literal.number(0)

    def _parse_term() -> Expr:
        left = _parse_factor()
        while _peek() and _peek().kind == "op" and _peek().value in "*/":  # type: ignore[union-attr]
            op_tok = _advance()
            right = _parse_factor()
            left = BinaryOp(left=left, op=op_tok.value, right=right)
        return left

    def _parse_add() -> Expr:
        left = _parse_term()
        while _peek() and _peek().kind == "op" and _peek().value in "+-":  # type: ignore[union-attr]
            op_tok = _advance()
            right = _parse_term()
            left = BinaryOp(left=left, op=op_tok.value, right=right)
        return left

    def _parse_cmp() -> Expr:
        left = _parse_add()
        t = _peek()
        if t is not None and t.kind == "op" and t.value in _COMPARISON_OPS:
            op_tok = _advance()
            # Normalise ``!=`` to ``<>``.
            op = "<>" if op_tok.value == "!=" else op_tok.value
            right = _parse_add()
            return BinaryOp(left=left, op=op, right=right)
        return left

    def _parse_not() -> Expr:
        if _is_op("NOT"):
            _advance()
            return UnaryOp(op="NOT", operand=_parse_not())
        return _parse_cmp()

    def _parse_and() -> Expr:
        left = _parse_not()
        while _is_op("AND"):
            _advance()
            right = _parse_not()
            left = BinaryOp(left=left, op="AND", right=right)
        return left

    def _parse_or() -> Expr:
        left = _parse_and()
        while _is_op("OR"):
            _advance()
            right = _parse_and()
            left = BinaryOp(left=left, op="OR", right=right)
        return left

    return _parse_or()

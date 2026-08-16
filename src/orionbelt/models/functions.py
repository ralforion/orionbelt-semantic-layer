"""Portable scalar-function catalog for OBML expressions.

An expression body — a computed column, a measure expression, a metric formula —
used to be pass-through: ``expr_parser`` turned any ``IDENT(`` into a
``FunctionCall`` and every dialect rendered ``name(args)`` verbatim. A model
written against one vendor therefore could not move to another, a misspelled
function validated clean and failed at the database, and Ossie/OSI interchange
carried expressions as opaque vendor SQL.

This module is the fix: a catalog of canonical, snake_case function names whose
*meaning* is pinned here rather than left to whichever engine runs the query.
The dialects render each canonical call into the engine's own spelling and
shape (``dialect/base.py::_render_function``), the validator checks names and
arity against the catalog (``parser/validator.py``), and anything outside the
catalog is still emitted verbatim — the escape hatch that keeps existing models
working.

Design decisions (``design/PLAN_portable_functions.md``):

* **The canonical form is DuckDB's, cross-checked against Postgres.** DuckDB
  accepted more probed candidates than any other engine and is the project's
  local reference engine, so "canonical" means something executable: every
  :class:`FunctionExample` below runs unchanged on DuckDB and returns the
  documented value, and every other dialect is asserted to produce that same
  value for the same canonical call.
* **Semantics are part of the spec, not the engine's business.** Where engines
  disagree on the *answer* rather than the spelling, the catalog states the rule
  and the renderer bends the engine to it — ``concat`` propagates NULL even on
  the engines that skip it, ``length`` counts characters even on the engines
  that count bytes.
* **Rewriting arguments is expected.** A catalog entry is not a rename table:
  ``position(needle, haystack)`` becomes ``POSITION(needle IN haystack)`` on
  most engines and ``STRPOS(haystack, needle)`` on BigQuery.

Argument *types* are deliberately not modelled. The compiler does no type
inference over expression bodies, so a declared argument type could only be
checked against a literal — arity and semantics are what the validator and the
renderers actually need. ``result_type`` is carried because it documents the
catalog for readers and for the reference surface.

Groups ship in phases; this module currently holds the string group.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Function groups, in the order they are presented to readers.
GROUP_STRING = "string"


@dataclass(frozen=True)
class FunctionExample:
    """A canonical call and the value the catalog guarantees it returns.

    ``call`` is an OBSL expression with literal arguments only, so it can be
    parsed by the expression parser, rendered by any dialect, and executed as
    ``SELECT <rendered>`` against every engine — which is exactly what the
    execution matrix in ``tests/integration/drift/vendor_exec`` does. An
    example is therefore a test case, not documentation that can drift.
    """

    call: str
    expect: str | int | float | bool | None


@dataclass(frozen=True)
class FunctionSpec:
    """One catalog entry: a canonical name, its arity, and its pinned meaning."""

    name: str
    signature: str
    group: str
    min_args: int
    max_args: int | None
    result_type: str
    summary: str
    semantics: str | None = None
    examples: tuple[FunctionExample, ...] = field(default_factory=tuple)

    def accepts(self, arg_count: int) -> bool:
        """Whether *arg_count* satisfies this entry's arity."""
        if arg_count < self.min_args:
            return False
        return self.max_args is None or arg_count <= self.max_args

    @property
    def arity_text(self) -> str:
        """Human-readable arity, for error messages ("2 or 3 arguments")."""
        if self.max_args is None:
            return f"at least {self.min_args} arguments"
        if self.max_args == self.min_args:
            plural = "" if self.min_args == 1 else "s"
            return f"exactly {self.min_args} argument{plural}"
        return f"{self.min_args} or {self.max_args} arguments"


_STRING_FUNCTIONS: tuple[FunctionSpec, ...] = (
    FunctionSpec(
        name="substring",
        signature="substring(x, start, len?)",
        group=GROUP_STRING,
        min_args=2,
        max_args=3,
        result_type="string",
        summary="Substring of *x* starting at *start*, at most *len* characters.",
        semantics="1-based; omitting *len* runs to the end of the string.",
        examples=(
            FunctionExample("substring('abcdef', 2, 3)", "bcd"),
            FunctionExample("substring('abcdef', 2)", "bcdef"),
        ),
    ),
    FunctionSpec(
        name="concat",
        signature="concat(a, b, ...)",
        group=GROUP_STRING,
        min_args=2,
        max_args=None,
        result_type="string",
        summary="Concatenate all arguments.",
        semantics=(
            "NULL propagates: if any argument is NULL the result is NULL, per the "
            "SQL standard. DuckDB, Postgres and Dremio skip NULL arguments instead, "
            "so their renderers rewrite the call."
        ),
        examples=(
            FunctionExample("concat('a', 'b', 'c')", "abc"),
            FunctionExample("concat('a', NULL, 'c')", None),
        ),
    ),
    FunctionSpec(
        name="upper",
        signature="upper(x)",
        group=GROUP_STRING,
        min_args=1,
        max_args=1,
        result_type="string",
        summary="Upper-case *x*.",
        semantics="Case mapping of non-ASCII characters follows the engine's collation.",
        examples=(FunctionExample("upper('aBc')", "ABC"),),
    ),
    FunctionSpec(
        name="lower",
        signature="lower(x)",
        group=GROUP_STRING,
        min_args=1,
        max_args=1,
        result_type="string",
        summary="Lower-case *x*.",
        semantics="Case mapping of non-ASCII characters follows the engine's collation.",
        examples=(FunctionExample("lower('AbC')", "abc"),),
    ),
    FunctionSpec(
        name="trim",
        signature="trim(x)",
        group=GROUP_STRING,
        min_args=1,
        max_args=1,
        result_type="string",
        summary="Strip leading and trailing whitespace.",
        semantics="Whitespace only — trimming a custom character set is not in v1.",
        examples=(FunctionExample("trim('  ab  ')", "ab"),),
    ),
    FunctionSpec(
        name="ltrim",
        signature="ltrim(x)",
        group=GROUP_STRING,
        min_args=1,
        max_args=1,
        result_type="string",
        summary="Strip leading whitespace.",
        semantics="Whitespace only — trimming a custom character set is not in v1.",
        examples=(FunctionExample("ltrim('  ab')", "ab"),),
    ),
    FunctionSpec(
        name="rtrim",
        signature="rtrim(x)",
        group=GROUP_STRING,
        min_args=1,
        max_args=1,
        result_type="string",
        summary="Strip trailing whitespace.",
        semantics="Whitespace only — trimming a custom character set is not in v1.",
        examples=(FunctionExample("rtrim('ab  ')", "ab"),),
    ),
    FunctionSpec(
        name="length",
        signature="length(x)",
        group=GROUP_STRING,
        min_args=1,
        max_args=1,
        result_type="int",
        summary="Number of characters in *x*.",
        semantics=(
            "Characters, not bytes. ClickHouse and MySQL count bytes in their "
            "``length``, so their renderers use the character-counting function."
        ),
        examples=(FunctionExample("length('äbcd')", 4),),
    ),
    FunctionSpec(
        name="replace",
        signature="replace(x, from, to)",
        group=GROUP_STRING,
        min_args=3,
        max_args=3,
        result_type="string",
        summary="Replace every occurrence of *from* in *x* with *to*.",
        semantics="All occurrences, not just the first.",
        examples=(FunctionExample("replace('abcab', 'ab', 'X')", "XcX"),),
    ),
    FunctionSpec(
        name="position",
        signature="position(needle, haystack)",
        group=GROUP_STRING,
        min_args=2,
        max_args=2,
        result_type="int",
        summary="1-based position of *needle* within *haystack*, 0 when absent.",
        semantics=(
            "Needle first — the argument order is fixed here rather than following "
            "the ANSI ``POSITION(needle IN haystack)`` form, which several engines "
            "do not parse."
        ),
        examples=(
            FunctionExample("position('cd', 'abcd')", 3),
            FunctionExample("position('zz', 'abcd')", 0),
        ),
    ),
    FunctionSpec(
        name="split_part",
        signature="split_part(x, delim, n)",
        group=GROUP_STRING,
        min_args=3,
        max_args=3,
        result_type="string",
        summary="The *n*-th field of *x* split on *delim*.",
        semantics=(
            "1-based; an *n* past the last field yields an empty string, not NULL. "
            "Behaviour for *n* < 1 is left to the engine and not part of the catalog."
        ),
        examples=(
            FunctionExample("split_part('a,b,c', ',', 2)", "b"),
            FunctionExample("split_part('a,b,c', ',', 9)", ""),
        ),
    ),
    FunctionSpec(
        name="lpad",
        signature="lpad(x, len, fill)",
        group=GROUP_STRING,
        min_args=3,
        max_args=3,
        result_type="string",
        summary="Pad *x* on the left with *fill* until it is *len* characters long.",
        semantics="*x* longer than *len* is truncated to *len*.",
        examples=(FunctionExample("lpad('7', 3, '0')", "007"),),
    ),
    FunctionSpec(
        name="rpad",
        signature="rpad(x, len, fill)",
        group=GROUP_STRING,
        min_args=3,
        max_args=3,
        result_type="string",
        summary="Pad *x* on the right with *fill* until it is *len* characters long.",
        semantics="*x* longer than *len* is truncated to *len*.",
        examples=(FunctionExample("rpad('7', 3, '0')", "700"),),
    ),
    FunctionSpec(
        name="starts_with",
        signature="starts_with(x, prefix)",
        group=GROUP_STRING,
        min_args=2,
        max_args=2,
        result_type="boolean",
        summary="Whether *x* begins with *prefix*.",
        semantics="Case-sensitive comparison.",
        examples=(
            FunctionExample("starts_with('abcd', 'ab')", True),
            FunctionExample("starts_with('abcd', 'bc')", False),
        ),
    ),
    FunctionSpec(
        name="ends_with",
        signature="ends_with(x, suffix)",
        group=GROUP_STRING,
        min_args=2,
        max_args=2,
        result_type="boolean",
        summary="Whether *x* ends with *suffix*.",
        semantics="Case-sensitive comparison.",
        examples=(
            FunctionExample("ends_with('abcd', 'cd')", True),
            FunctionExample("ends_with('abcd', 'bc')", False),
        ),
    ),
)


FUNCTION_CATALOG: dict[str, FunctionSpec] = {spec.name: spec for spec in _STRING_FUNCTIONS}
"""Every catalog entry, keyed by its canonical (lowercase) name."""


def lookup_function(name: str) -> FunctionSpec | None:
    """The catalog entry *name* refers to, or ``None`` if it is not in the catalog.

    Case-insensitive: OBML expressions are written both ``SUBSTRING(...)`` and
    ``substring(...)``, and SQL treats the two as one function.
    """
    return FUNCTION_CATALOG.get(name.lower())


def catalog_names() -> list[str]:
    """Canonical names, sorted — for error hints and the reference surface."""
    return sorted(FUNCTION_CATALOG)

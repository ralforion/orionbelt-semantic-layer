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

Groups ship in phases; this module currently holds the string, numeric and
conditional groups. Date/time is next, and is the one where every dialect needs
a renderer rather than a few.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Function groups, in the order they are presented to readers.
GROUP_STRING = "string"
GROUP_NUMERIC = "numeric"
GROUP_CONDITIONAL = "conditional"


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
    """OBML abstract type of the result, or one of two words for the entries
    whose result type is not fixed: ``numeric`` (int or float, following the
    argument) and ``argument`` (whatever the arguments are, including strings
    and dates). The *value* is what the catalog pins; the SQL type an engine
    delivers it in is its own business, and ``sign`` alone comes back as an
    integer on DuckDB, a numeric on Postgres and a float on BigQuery."""

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


def _simple_numeric(
    name: str, summary: str, example: FunctionExample, *, result_type: str = "float"
) -> FunctionSpec:
    """A single-argument numeric entry every engine spells the same way.

    Seven of the numeric entries differ in nothing but their name, and writing
    each out in full buried the four that carry real per-dialect behaviour.
    """
    return FunctionSpec(
        name=name,
        signature=f"{name}(x)",
        group=GROUP_NUMERIC,
        min_args=1,
        max_args=1,
        result_type=result_type,
        summary=summary,
        examples=(example,),
    )


_NUMERIC_FUNCTIONS: tuple[FunctionSpec, ...] = (
    _simple_numeric(
        "abs",
        "Absolute value of *x*.",
        FunctionExample("abs(-3)", 3),
        result_type="numeric",
    ),
    _simple_numeric(
        "sign",
        "-1, 0 or 1 as *x* is negative, zero or positive.",
        FunctionExample("sign(-3)", -1),
        result_type="int",
    ),
    _simple_numeric(
        "floor", "Largest integer not greater than *x*.", FunctionExample("floor(-1.2)", -2)
    ),
    _simple_numeric("ceil", "Smallest integer not less than *x*.", FunctionExample("ceil(1.2)", 2)),
    _simple_numeric("sqrt", "Square root of *x*.", FunctionExample("sqrt(4)", 2)),
    _simple_numeric("ln", "Natural logarithm of *x*.", FunctionExample("ln(1)", 0)),
    _simple_numeric("exp", "*e* raised to the power of *x*.", FunctionExample("exp(0)", 1)),
    FunctionSpec(
        name="power",
        signature="power(base, exponent)",
        group=GROUP_NUMERIC,
        min_args=2,
        max_args=2,
        result_type="float",
        summary="*base* raised to the power of *exponent*.",
        examples=(FunctionExample("power(2, 10)", 1024),),
    ),
    FunctionSpec(
        name="round",
        signature="round(x, n?)",
        group=GROUP_NUMERIC,
        min_args=1,
        max_args=2,
        result_type="float",
        summary="Round *x* to *n* decimal places, or to a whole number.",
        semantics=(
            "Ties round away from zero: 2.5 is 3 and -2.5 is -3. ClickHouse "
            "rounds ties to even (2.5 is 2), so its renderer rewrites the call "
            "arithmetically rather than using the native ROUND."
        ),
        examples=(
            FunctionExample("round(2.5)", 3),
            FunctionExample("round(-2.5)", -3),
            FunctionExample("round(0.5)", 1),
            FunctionExample("round(2.345, 2)", 2.35),
        ),
    ),
    FunctionSpec(
        name="trunc",
        signature="trunc(x, n?)",
        group=GROUP_NUMERIC,
        min_args=1,
        max_args=2,
        result_type="float",
        summary="Truncate *x* to *n* decimal places, or to a whole number.",
        semantics=(
            "Toward zero, so -1.9 truncates to -1 where floor would give -2. "
            "MySQL and Dremio spell it TRUNCATE and require the digit count; "
            "Databricks has no numeric truncation at all and gets a rewrite."
        ),
        examples=(
            FunctionExample("trunc(1.9)", 1),
            FunctionExample("trunc(-1.9)", -1),
            FunctionExample("trunc(2.345, 2)", 2.34),
        ),
    ),
    FunctionSpec(
        name="mod",
        signature="mod(a, b)",
        group=GROUP_NUMERIC,
        min_args=2,
        max_args=2,
        result_type="numeric",
        summary="Remainder of *a* divided by *b*.",
        semantics="The result takes the sign of the dividend: mod(-7, 3) is -1.",
        examples=(
            FunctionExample("mod(7, 3)", 1),
            FunctionExample("mod(-7, 3)", -1),
        ),
    ),
    FunctionSpec(
        name="div",
        signature="div(a, b)",
        group=GROUP_NUMERIC,
        min_args=2,
        max_args=2,
        result_type="int",
        summary="Integer division of *a* by *b*.",
        semantics=(
            "Truncates toward zero, so div(-7, 2) is -3 rather than -4. This is "
            "the only way to ask for integer division: ``/`` is left to the "
            "engine, and Postgres alone reads 7 / 2 as 3. No engine spells this "
            "``div(a, b)`` and DuckDB has no such function at all, so the name is "
            "an OBSL one that every dialect renders: ``a // b`` on DuckDB, "
            "``intDiv`` on ClickHouse, the ``DIV`` operator on MySQL and "
            "Databricks, ``TRUNC(a / b)`` on Snowflake."
        ),
        examples=(
            FunctionExample("div(7, 2)", 3),
            FunctionExample("div(-7, 2)", -3),
        ),
    ),
    FunctionSpec(
        name="log",
        signature="log(base, x)",
        group=GROUP_NUMERIC,
        min_args=2,
        max_args=2,
        result_type="float",
        summary="Logarithm of *x* in the given *base*.",
        semantics=(
            "Base first. BigQuery's own LOG takes them the other way round and "
            "ClickHouse has no two-argument form, so both are rewritten. The "
            "single-argument ``log`` is deliberately not in the catalog: it is "
            "base 10 on DuckDB and Postgres and natural on ClickHouse, MySQL and "
            "BigQuery, which is a silent factor of 2.3. Use ``ln(x)`` for the "
            "natural logarithm."
        ),
        examples=(
            FunctionExample("log(10, 100)", 2),
            FunctionExample("log(2, 8)", 3),
        ),
    ),
)


_CONDITIONAL_FUNCTIONS: tuple[FunctionSpec, ...] = (
    FunctionSpec(
        name="coalesce",
        signature="coalesce(a, b, ...)",
        group=GROUP_CONDITIONAL,
        min_args=2,
        max_args=None,
        result_type="argument",
        summary="The first argument that is not NULL, or NULL if none is.",
        examples=(
            FunctionExample("coalesce(NULL, 'x')", "x"),
            FunctionExample("coalesce(NULL, NULL)", None),
        ),
    ),
    FunctionSpec(
        name="nullif",
        signature="nullif(a, b)",
        group=GROUP_CONDITIONAL,
        min_args=2,
        max_args=2,
        result_type="argument",
        summary="NULL when *a* equals *b*, otherwise *a*.",
        examples=(
            FunctionExample("nullif('a', 'a')", None),
            FunctionExample("nullif('a', 'b')", "a"),
        ),
    ),
    FunctionSpec(
        name="greatest",
        signature="greatest(a, b, ...)",
        group=GROUP_CONDITIONAL,
        min_args=2,
        max_args=None,
        result_type="argument",
        summary="The largest argument.",
        semantics=(
            "NULL propagates, as it does for ``concat``: a NULL argument makes "
            "the result NULL rather than being skipped. The engines split four "
            "to three on this, with DuckDB, Postgres, ClickHouse and Databricks "
            "skipping NULLs, so those four are rewritten with a guard. To take "
            "the largest of the values that are present, say so: "
            "``greatest(coalesce(a, 0), coalesce(b, 0))``."
        ),
        examples=(
            FunctionExample("greatest(1, 2, 3)", 3),
            FunctionExample("greatest(1, NULL, 3)", None),
        ),
    ),
    FunctionSpec(
        name="least",
        signature="least(a, b, ...)",
        group=GROUP_CONDITIONAL,
        min_args=2,
        max_args=None,
        result_type="argument",
        summary="The smallest argument.",
        semantics="NULL propagates, exactly as for ``greatest``.",
        examples=(
            FunctionExample("least(3, 2, 1)", 1),
            FunctionExample("least(3, NULL, 1)", None),
        ),
    ),
)


FUNCTION_CATALOG: dict[str, FunctionSpec] = {
    spec.name: spec for spec in (*_STRING_FUNCTIONS, *_NUMERIC_FUNCTIONS, *_CONDITIONAL_FUNCTIONS)
}
"""Every catalog entry, keyed by its canonical (lowercase) name."""


def markdown_prose(text: str) -> str:
    """The catalog's prose reads as a Python docstring, where ``x`` is code.

    Every surface that publishes it — the JSON reference endpoint, the OBML
    markdown reference — is read as markdown, where the doubled backticks are
    literal characters. Converted once here so the two cannot disagree.
    """
    return text.replace("``", "`")


def lookup_function(name: str) -> FunctionSpec | None:
    """The catalog entry *name* refers to, or ``None`` if it is not in the catalog.

    Case-insensitive: OBML expressions are written both ``SUBSTRING(...)`` and
    ``substring(...)``, and SQL treats the two as one function.
    """
    return FUNCTION_CATALOG.get(name.lower())


def catalog_names() -> list[str]:
    """Canonical names, sorted — for error hints and the reference surface."""
    return sorted(FUNCTION_CATALOG)

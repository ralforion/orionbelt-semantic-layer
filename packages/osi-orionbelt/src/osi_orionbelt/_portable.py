"""Portable Apache Ossie SQL for OBML measures and metrics.

An exported metric expression is what every other Ossie consumer reads, so it
has to compute what OrionBelt computes. OBML carries semantics in structure
(measure ``filters``, ``total``, synthesized counts, metric-on-metric
references) that the expression must spell out:

* column references are ``<dataset>.<field>``: the OBML data object and column
  names, which are the Ossie dataset and field names;
* measure ``filters`` become ``AGG(CASE WHEN <cond> THEN <arg> END)``, the
  spec's portable filtered aggregation, mirroring the compiler's own rendering;
* ``total: true`` becomes the grand-total window the compiler emits, e.g.
  ``SUM(SUM(x)) OVER ()``;
* ``{[Name]}`` references resolve to the referenced measure, synthesized count
  or metric, inlined.

What has no faithful single-expression form raises :class:`NotPortableError` so the
caller can leave it out of the Ossie document instead of guessing.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from osi_orionbelt._sql_reserved import DUCKDB_REJECTED_WORDS, SQL_RESERVED_WORDS

_MEASURE_REF = re.compile(r"\{\[([^\]]+)\]\}")
_COLUMN_REF = re.compile(r"\{\[([^\]]+)\]\.\[([^\]]+)\]\}")

_DEFAULT_COUNT_PATTERN = "{object} Count"

# Measure options whose value depends on the query (its grouping or filters),
# which a standalone Ossie metric expression cannot see.
_QUERY_DEPENDENT_OPTIONS = {
    "grain": "a grain override is evaluated relative to the query's dimensions",
    "filterContext": "a filter context changes which query filters apply",
    "anchor": "an anchored expression spans independent facts",
}

# How the compiler re-aggregates a ``total: true`` measure over the grouped
# result (``compiler/total_wrap.py``); anything not listed re-aggregates by SUM.
_TOTAL_REAGG = {"min": "MIN", "max": "MAX"}

# A time-grain dimension declaring one of these ``resultType``s is cast back to
# it after truncation, as the compiler does (``compiler/resolution.py``), so the
# query groups by the cast expression.
_TEMPORAL_CASTS = {"date": "DATE", "timestamp": "TIMESTAMP", "time": "TIME"}

_CUMULATIVE_FUNCS = {"sum", "avg", "min", "max", "count"}
_GRAIN_TO_DATE = {"year", "quarter", "month", "week"}
_WINDOW_FUNCS = {
    "rank",
    "dense_rank",
    "row_number",
    "ntile",
    "lag",
    "lead",
    "first_value",
    "last_value",
    *_CUMULATIVE_FUNCS,
}

# Filter operators whose OSSIE_SQL_2026 form differs from the ANSI one.
_PATTERN_OPERATORS = {
    "contains",
    "notcontains",
    "starts_with",
    "ends_with",
    "like",
    "notlike",
    "ilike",
    "notilike",
}

_COMPARISONS = {
    "equals": "=",
    "notequals": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


class NotPortableError(Exception):
    """An OBML measure or metric has no faithful Apache Ossie expression."""


def sql_ident(name: str) -> str:
    """Render *name* as a double-quoted identifier.

    For physical SQL, such as a nested object's source query, where the exact
    case of a table or column name matters: Postgres folds a bare ``Key`` to
    ``key``. A reference to an Ossie dataset or field uses :func:`ossie_ident`.
    """
    return '"' + name.replace('"', '""') + '"'


_REGULAR_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@cache
def needs_quoting(name: str) -> bool:
    """Whether *name* cannot be a regular (unquoted) identifier in an export.

    Not identifier-shaped, a SQL:2023 reserved word, a word DuckDB refuses
    bare, or one sqlglot does not read as ``<name>.<name>``: what runs and
    validates these exports must parse the reference too.
    """
    if not _REGULAR_IDENTIFIER.fullmatch(name):
        return True
    if name.lower() in SQL_RESERVED_WORDS | DUCKDB_REJECTED_WORDS:
        return True
    try:
        node = sqlglot.parse_one(f"{name}.{name}")
    except sqlglot.errors.SqlglotError:
        return True
    return not (isinstance(node, exp.Column) and node.table == name and node.name == name)


def is_regular(name: str) -> bool:
    """Whether *name* can be an ANSI regular identifier: identifier-shaped and
    not a SQL:2023 reserved word.

    The expression language normalizes a regular identifier to upper case; any
    other name is matched as written.
    """
    return bool(_REGULAR_IDENTIFIER.fullmatch(name)) and name.lower() not in SQL_RESERVED_WORDS


def case_collisions(names: Iterable[str]) -> frozenset[str]:
    """The names that equal another one regardless of case.

    A bare identifier matches regardless of case, so ``Amount`` and ``amount``
    would be one reference.
    """
    names = list(dict.fromkeys(names))
    folded = Counter(name.upper() for name in names)
    return frozenset(name for name in names if folded[name.upper()] > 1)


def quoted_name(name: str, collisions: frozenset[str] = frozenset()) -> str | None:
    """The text a reference to *name* quotes, or ``None`` when it is written bare.

    The expression language follows ANSI SQL (``core-spec/expression_language.md``,
    "Namespacing and Identifier Resolution"): a regular identifier is normalized
    to upper case, a quoted one is taken as written. A regular name is written
    bare. One that DuckDB or sqlglot will not read bare, such as ``Qualify``, is
    quoted in its normalized form, ``"QUALIFY"``, which matches it. A name that
    cannot be regular (a space, a leading digit, a reserved word such as
    ``Order``) is matched as written, so it is quoted as written.

    Names in *collisions* differ only by case, so the language cannot tell them
    apart; each is quoted as written, which a reader that matches a quoted name
    exactly, as this converter does, resolves.
    """
    if name in collisions or not is_regular(name):
        return name
    if needs_quoting(name):
        return name.upper()
    return None


def ossie_ident(name: str, collisions: frozenset[str] = frozenset()) -> str:
    """*name* as an identifier in an Apache Ossie expression (see :func:`quoted_name`)."""
    text = quoted_name(name, collisions)
    return name if text is None else sql_ident(text)


def matching_names(ref: str, quoted: bool, names: Iterable[str]) -> list[str]:
    """The declared *names* the reference *ref* (unquoted text) matches.

    A quoted reference matches the name it spells exactly, else a regular name
    whose normalized form it spells (``"QUALIFY"`` matches ``Qualify``). A bare
    one matches regardless of case. More than one match is ambiguous.
    """
    names = list(names)
    if quoted:
        exact = [n for n in names if n == ref]
        return exact or [n for n in names if is_regular(n) and n.upper() == ref]
    return [n for n in names if n.upper() == ref.upper()]


def _string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _scalar_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    return _string_literal(str(value))


def _filter_literal(fv: dict[str, Any], ossie: bool = False) -> str:
    """Render a typed OBML ``FilterValue`` as the literal the compiler compares against.

    ``OSSIE_SQL_2026`` has no plain ``TIMESTAMP '...'`` literal, so with *ossie*
    a timestamp is a ``CAST``.
    """
    if fv.get("isNull"):
        return "NULL"
    data_type = fv.get("dataType")
    if data_type == "int":
        return _scalar_literal(fv.get("valueInt"))
    if data_type == "float":
        return _scalar_literal(fv.get("valueFloat"))
    if data_type == "decimal":
        return _decimal_literal(fv)
    if data_type == "boolean":
        return _scalar_literal(fv.get("valueBoolean"))
    if data_type == "date" and fv.get("valueDate") is not None:
        return f"DATE {_string_literal(str(fv['valueDate']))}"
    if data_type == "timestamp" and fv.get("valueDate") is not None:
        text = _string_literal(str(fv["valueDate"]))
        return f"CAST({text} AS TIMESTAMP)" if ossie else f"TIMESTAMP {text}"
    return _scalar_literal(fv.get("valueString"))


def decimal_filter_value(fv: dict[str, Any]) -> Any:
    """What a ``decimal`` filter value was given as: ``valueFloat``, else the
    digits as text in ``valueString``, as OrionBelt reads it."""
    value = fv.get("valueFloat")
    return fv.get("valueString") if value is None else value


def _decimal_literal(fv: dict[str, Any]) -> str:
    """A ``decimal`` filter value as the exact number it names.

    ``valueString`` holds the digits as text, which no JSON or YAML writer
    rounds; a float renders its shortest text (``converter`` warns about it).
    Text that is not a finite number has no literal and is refused rather
    than spliced into the SQL.
    """
    value = decimal_filter_value(fv)
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        raise NotPortableError(f"its decimal filter value {value!r} is not a number")
    try:
        exact = Decimal(str(value).strip())
    except InvalidOperation:
        exact = None
    if exact is None or not exact.is_finite():
        raise NotPortableError(f"its decimal filter value {value!r} is not a number")
    return format(exact, "f")


def _filter_text(fv: dict[str, Any]) -> str:
    """The raw text of a filter value, for LIKE patterns."""
    for key in ("valueString", "valueInt", "valueFloat", "valueDate", "valueBoolean"):
        if fv.get(key) is not None:
            return str(fv[key])
    return ""


def _escape_clause(pattern: str) -> str:
    """``ESCAPE '\\'`` when *pattern* uses OBML's backslash escape, else nothing.

    ANSI LIKE has no default escape character.
    """
    return " ESCAPE '\\'" if "\\" in pattern else ""


def _like(col: str, pattern_parts: tuple[str, str], value: str, negated: bool) -> str:
    """``col [NOT] LIKE`` with *value* matched literally between the wildcards."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    op = "NOT LIKE" if negated else "LIKE"
    pattern = _string_literal(pattern_parts[0] + escaped + pattern_parts[1])
    return f"{col} {op} {pattern}{_escape_clause(escaped)}"


def synthesized_counts(obml: dict[str, Any]) -> dict[str, str]:
    """Map each synthesized count measure's name to its data object.

    Mirrors ``orionbelt.models.synthesis``: ``exposeCounts`` and per-object
    ``countable`` opt out, nested objects never get one, the name resolves
    ``countLabel`` > ``countLabelPattern`` > ``"{object} Count"``, and a
    declared measure of the same name wins.
    """
    if obml.get("exposeCounts") is False:
        return {}
    pattern = obml.get("countLabelPattern") or _DEFAULT_COUNT_PATTERN
    declared = obml.get("measures") or {}
    counts: dict[str, str] = {}
    for key, obj in (obml.get("dataObjects") or {}).items():
        if obj.get("countable") is False or obj.get("nestedIn"):
            continue
        name = (obj.get("countLabel") or pattern).replace("{object}", key)
        if name not in declared and name not in counts:
            counts[name] = key
    return counts


class PortableRenderer:
    """Render OBML measures and metrics as portable Ossie SQL expressions.

    By default the expressions are ANSI SQL. With *ossie* they use the forms of
    Apache Ossie's ``OSSIE_SQL_2026`` language where the two differ: ``CONTAINS``,
    ``STARTSWITH`` and ``ENDSWITH`` instead of ``LIKE ... ESCAPE``, ``ILIKE``,
    and a ``CAST`` for a timestamp literal. A filter that needs ``ESCAPE`` has
    no such form and raises :class:`NotPortableError`.
    """

    def __init__(self, obml: dict[str, Any], *, ossie: bool = False) -> None:
        self.ossie = ossie
        self.data_objects: dict[str, Any] = obml.get("dataObjects") or {}
        self.dimensions: dict[str, Any] = obml.get("dimensions") or {}
        self.measures: dict[str, Any] = obml.get("measures") or {}
        self.metrics: dict[str, Any] = obml.get("metrics") or {}
        self.counts = synthesized_counts(obml)
        self._resolving: set[str] = set()
        # Names a bare reference could not tell apart: dataset names across the
        # model, field names within each data object.
        self._dataset_collisions = case_collisions(self.data_objects)
        self._field_collisions = {
            name: case_collisions(obj.get("columns") or {})
            for name, obj in self.data_objects.items()
        }

    # ── references ────────────────────────────────────────────────────

    def column(self, data_object: str, column: str) -> str:
        """``<dataset>.<field>`` for an OBML column."""
        obj = self.data_objects.get(data_object)
        if obj is None:
            raise NotPortableError(f"references unknown data object '{data_object}'")
        dataset = ossie_ident(data_object, self._dataset_collisions)
        field = ossie_ident(column, self._field_collisions.get(data_object, frozenset()))
        return f"{dataset}.{field}"

    def _column_ref(self, ref: dict[str, Any]) -> str:
        return self.column(ref.get("dataObject", ""), ref.get("column", ""))

    def _dimension(self, name: str) -> str:
        """The dimension exactly as the query groups by it.

        A ``timeGrain`` truncates the column, and a temporal ``resultType`` casts
        the truncation back to that type, as the compiler renders it; ordering
        by anything else is not a grouped expression.
        """
        dim = self.dimensions.get(name)
        if dim is None:
            raise NotPortableError(f"references unknown dimension '{name}'")
        column = self.column(dim.get("dataObject", ""), dim.get("column", ""))
        grain = dim.get("timeGrain")
        if not grain:
            return column
        truncated = f"DATE_TRUNC({_string_literal(grain)}, {column})"
        cast = _TEMPORAL_CASTS.get(str(dim.get("resultType", "")).lower())
        return f"CAST({truncated} AS {cast})" if cast else truncated

    def _expression(self, template: str) -> str:
        return _COLUMN_REF.sub(lambda m: self.column(m.group(1), m.group(2)), template)

    # ── measures ──────────────────────────────────────────────────────

    def measure(self, name: str) -> str:
        """The expression for a declared or synthesized measure."""
        if name in self.measures:
            return self._declared_measure(name, self.measures[name])
        if name in self.counts:
            return self._count(self.counts[name])
        raise NotPortableError(f"references unknown measure '{name}'")

    def _count(self, data_object: str) -> str:
        obj = self.data_objects[data_object]
        pk = [c for c, col in (obj.get("columns") or {}).items() if col.get("primaryKey")]
        if len(pk) != 1:
            raise NotPortableError(
                f"the row count of '{data_object}' needs a single-column primary key "
                f"to anchor COUNT on"
            )
        return f"COUNT({self.column(data_object, pk[0])})"

    def _declared_measure(self, name: str, m: dict[str, Any]) -> str:
        for option, why in _QUERY_DEPENDENT_OPTIONS.items():
            if m.get(option):
                raise NotPortableError(f"measure '{name}' uses '{option}': {why}")
        agg = str(m.get("aggregation", "sum")).lower()
        if agg == "measure":
            raise NotPortableError(f"measure '{name}' is resolved by a Databricks Metric View")
        if agg in ("first", "last"):
            end = "greatest" if agg == "last" else "least"
            raise NotPortableError(
                f"measure '{name}' takes the value at the {end} key, which ANSI SQL has no "
                "aggregate for (each engine spells it its own way)"
            )

        if m.get("columns"):
            args = [self._column_ref(c) for c in m["columns"]]
        elif m.get("expression"):
            args = [self._expression(m["expression"])]
        else:
            raise NotPortableError(f"measure '{name}' has no columns or expression")

        if m.get("filters"):
            condition = self._filters(m["filters"])
            args = [f"CASE WHEN {condition} THEN {a} END" for a in args]

        distinct = "DISTINCT " if m.get("distinct") or agg == "count_distinct" else ""
        func = "COUNT" if agg == "count_distinct" else agg.upper()
        if agg == "listagg" and m.get("delimiter") is not None:
            args.append(_string_literal(str(m["delimiter"])))
        within = ""
        if m.get("withinGroup"):
            wg = m["withinGroup"]
            direction = "DESC" if str(wg.get("order", "ASC")).upper() == "DESC" else "ASC"
            within = f" WITHIN GROUP (ORDER BY {self._column_ref(wg['column'])} {direction})"
        sql = f"{func}({distinct}{', '.join(args)}){within}"
        if agg in ("percentile_cont", "percentile_disc"):
            # An ordered-set aggregate: the fraction is its argument, the
            # value it reads the ordering.
            fraction = format(Decimal(str(m["percentile"])), "f")
            sql = f"{func}({fraction}) WITHIN GROUP (ORDER BY {args[0]})"

        if m.get("total"):
            if agg == "avg":
                arg = args[0]
                sql = f"(SUM(SUM({arg})) OVER () / SUM(COUNT({arg})) OVER ())"
            else:
                sql = f"{_TOTAL_REAGG.get(agg, 'SUM')}({sql}) OVER ()"
        if m.get("defaultValue") is not None:
            sql = f"COALESCE({sql}, {_scalar_literal(m['defaultValue'])})"
        return sql

    # ── measure filters (mirrors compiler/filters.py) ─────────────────

    def _filters(self, items: list[Any]) -> str:
        return " AND ".join(self._filter_item(i, top=True) for i in items)

    def _filter_item(self, item: dict[str, Any], top: bool = False) -> str:
        if "filters" in item:
            logic = " OR " if str(item.get("logic", "and")).lower() == "or" else " AND "
            inner = logic.join(self._filter_item(i) for i in item["filters"])
            if not inner:
                raise NotPortableError("an empty measure filter group")
            return f"NOT ({inner})" if item.get("negated") else f"({inner})"
        leaf = self._filter_leaf(item)
        return leaf if top else f"({leaf})"

    def _filter_leaf(self, f: dict[str, Any]) -> str:
        col = self._column_ref(f.get("column") or {})
        op = str(f.get("operator", "")).lower()
        values = f.get("values") or []
        first = _filter_literal(values[0], self.ossie) if values else "NULL"
        if op in _COMPARISONS:
            return f"{col} {_COMPARISONS[op]} {first}"
        if op in ("inlist", "notinlist"):
            keyword = "NOT IN" if op == "notinlist" else "IN"
            literals = ", ".join(_filter_literal(v, self.ossie) for v in values)
            return f"{col} {keyword} ({literals})"
        if op == "set":
            return f"{col} IS NOT NULL"
        if op == "notset":
            return f"{col} IS NULL"
        text = _filter_text(values[0]) if values else ""
        if self.ossie and op in _PATTERN_OPERATORS:
            return self._ossie_pattern(col, op, text)
        if op in ("contains", "notcontains"):
            return _like(col, ("%", "%"), text, negated=op == "notcontains")
        if op == "starts_with":
            return _like(col, ("", "%"), text, negated=False)
        if op == "ends_with":
            return _like(col, ("%", ""), text, negated=False)
        if op in ("like", "notlike"):
            keyword = "NOT LIKE" if op == "notlike" else "LIKE"
            return f"{col} {keyword} {_string_literal(text)}{_escape_clause(text)}"
        if op in ("ilike", "notilike"):
            # ILIKE is not ANSI; LOWER of both sides is.
            keyword = "NOT LIKE" if op == "notilike" else "LIKE"
            return f"LOWER({col}) {keyword} LOWER({_string_literal(text)}){_escape_clause(text)}"
        if op in ("between", "notbetween"):
            if len(values) < 2:
                return f"{col} {'<>' if op == 'notbetween' else '='} {first}"
            keyword = "NOT BETWEEN" if op == "notbetween" else "BETWEEN"
            return f"{col} {keyword} {first} AND {_filter_literal(values[1], self.ossie)}"
        raise NotPortableError(f"measure filter operator '{op}' has no portable form")

    @staticmethod
    def _ossie_pattern(col: str, op: str, text: str) -> str:
        """A pattern filter in its ``OSSIE_SQL_2026`` form.

        The substring operators match *text* literally, so they need no escaping;
        a ``LIKE`` pattern that relies on OBML's backslash escape has no form,
        because the language has no ``ESCAPE`` clause.
        """
        literal = _string_literal(text)
        if op in ("contains", "notcontains"):
            negation = "NOT " if op == "notcontains" else ""
            return f"{negation}CONTAINS({col}, {literal})"
        if op == "starts_with":
            return f"STARTSWITH({col}, {literal})"
        if op == "ends_with":
            return f"ENDSWITH({col}, {literal})"
        if "\\" in text:
            raise NotPortableError(
                f"pattern {literal} needs an ESCAPE clause, which OSSIE_SQL_2026 does not have"
            )
        keyword = op.removeprefix("not").upper()
        return f"{col} {'NOT ' if op.startswith('not') else ''}{keyword} {literal}"

    # ── metrics ───────────────────────────────────────────────────────

    def metric(self, name: str) -> str:
        """The expression for an OBML metric."""
        if name in self._resolving:
            raise NotPortableError(f"metric '{name}' references itself")
        self._resolving.add(name)
        try:
            return self._metric(name, self.metrics[name])
        finally:
            self._resolving.discard(name)

    def grouping(self, name: str) -> tuple[str, ...] | None:
        """The dimensions a query must group by, and only those, for *name* to be right.

        OrionBelt splits a cumulative metric by every other dimension of the
        query, which an expression cannot name: its window is right only in a
        query grouped by its time dimension and ``partitionBy``, in any order.
        Sorted; ``None`` when *name* reaches no cumulative metric. Call it once
        :meth:`metric` has rendered *name*, so a reference cycle has already
        been refused.
        """
        groupings = self._groupings(name)
        if len(groupings) > 1:
            raise NotPortableError(
                f"metric '{name}' combines cumulative metrics that need different groupings, "
                f"so no query is right for all of them"
            )
        return tuple(sorted(next(iter(groupings)))) if groupings else None

    def _groupings(self, name: str) -> set[frozenset[str]]:
        met = self.metrics.get(name)
        if met is None:
            return set()
        if met.get("type") == "cumulative":
            return {frozenset([met.get("timeDimension", ""), *(met.get("partitionBy") or [])])}
        refs = _MEASURE_REF.findall(met.get("expression") or "")
        return set().union(*(self._groupings(r) for r in refs))

    def _has_window(self, name: str, visiting: frozenset[str] = frozenset()) -> bool:
        """Whether the expression for *name* already contains a window call.

        Raises :class:`NotPortableError` on a reference cycle, direct or not.
        """
        if name in visiting:
            raise NotPortableError(f"metric '{name}' is part of a reference cycle")
        if name in self.metrics:
            met = self.metrics[name]
            if met.get("type") in ("cumulative", "window"):
                return True
            refs = _MEASURE_REF.findall(met.get("expression") or "")
            return any(self._has_window(r, visiting | {name}) for r in refs)
        return bool(self.measures.get(name, {}).get("total"))

    def _windowed(self, name: str, ref: str) -> str:
        """The SQL for *ref*, which a window of metric *name* aggregates over.

        Window calls cannot nest, so a reference that already carries one (a
        grand total, or a cumulative or window metric) needs another query
        layer that one expression does not have.
        """
        if self._has_window(ref):
            raise NotPortableError(
                f"metric '{name}' applies a window over '{ref}', which is itself a window; "
                f"nested window calls need another query layer"
            )
        return self._reference(ref)

    def _reference(self, name: str) -> str:
        if name in self.metrics:
            return f"({self.metric(name)})"
        return self.measure(name)

    def _metric(self, name: str, met: dict[str, Any]) -> str:
        kind = met.get("type", "derived")
        if kind == "cumulative":
            return self._cumulative(name, met)
        if kind == "window":
            return self._window(name, met)
        if kind == "period_over_period":
            raise NotPortableError(
                f"metric '{name}' compares against a period shifted on a date spine, "
                f"which one expression cannot reproduce when periods are missing"
            )
        if kind == "reaggregate":
            raise NotPortableError(
                f"metric '{name}' aggregates '{met.get('measure')}' per group and then "
                f"aggregates those values again, which needs a second query layer"
            )
        template = met.get("expression")
        if not template:
            raise NotPortableError(f"metric '{name}' has no expression")
        return _MEASURE_REF.sub(lambda m: self._reference(m.group(1)), template)

    def _partitions(self, met: dict[str, Any]) -> list[str]:
        return [self._dimension(d) for d in met.get("partitionBy") or []]

    def _cumulative(self, name: str, met: dict[str, Any]) -> str:
        if met.get("windowSpan"):
            raise NotPortableError(
                f"metric '{name}' re-aggregates its measure's rows over a span of time "
                f"ending at each period, which a window over one row per period cannot do"
            )
        if met.get("window") is not None:
            raise NotPortableError(
                f"metric '{name}' rolls over the last {met['window']} periods of the calendar, "
                f"counting a period without rows, which a frame over the rows that exist cannot do"
            )
        func = str(met.get("cumulativeType", "sum")).lower()
        if func not in _CUMULATIVE_FUNCS or not met.get("measure"):
            raise NotPortableError(f"metric '{name}' is an incomplete cumulative metric")
        inner = self._windowed(name, met["measure"])
        time = self._dimension(met.get("timeDimension", ""))
        partitions = self._partitions(met)
        frame = "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"
        grain = met.get("grainToDate")
        if grain:
            if grain not in _GRAIN_TO_DATE:
                raise NotPortableError(f"metric '{name}' has unknown grainToDate '{grain}'")
            partitions.insert(0, f"DATE_TRUNC({_string_literal(grain)}, {time})")
        partition = f"PARTITION BY {', '.join(partitions)} " if partitions else ""
        return f"{func.upper()}({inner}) OVER ({partition}ORDER BY {time} {frame})"

    def _window(self, name: str, met: dict[str, Any]) -> str:
        """Mirror ``compiler/window_wrap.py``: ranking orders by the measure, else by time."""
        func = str(met.get("windowFunction", "")).lower()
        if func not in _WINDOW_FUNCS:
            raise NotPortableError(f"metric '{name}' has unknown windowFunction '{func}'")
        if func in _CUMULATIVE_FUNCS:
            raise NotPortableError(
                f"metric '{name}' aggregates a frame of rows split by every other dimension "
                f"of the query, which one expression cannot name"
            )
        direction = "DESC" if str(met.get("orderDirection", "desc")).lower() == "desc" else "ASC"
        inner = self._windowed(name, met["measure"]) if met.get("measure") else None
        time = self._dimension(met["timeDimension"]) if met.get("timeDimension") else None

        args: list[str] = []
        order: str | None = None
        if func in ("lag", "lead"):
            if inner is None or time is None:
                raise NotPortableError(f"metric '{name}' needs a measure and a timeDimension")
            args = [inner, str(int(met.get("offset") or 1))]
            if met.get("defaultValue") is not None:
                args.append(_scalar_literal(met["defaultValue"]))
            order = f"{time} ASC"
        elif func in ("first_value", "last_value"):
            if inner is None:
                raise NotPortableError(f"metric '{name}' needs a measure")
            args = [inner]
            order = f"{time} {direction}" if time else None
        else:
            if func == "ntile":
                if met.get("buckets") is None:
                    raise NotPortableError(f"metric '{name}' needs buckets")
                args = [str(int(met["buckets"]))]
            key = inner or time
            order = f"{key} {direction}" if key else None

        clauses = []
        partitions = self._partitions(met)
        if partitions:
            clauses.append(f"PARTITION BY {', '.join(partitions)}")
        if order:
            clauses.append(f"ORDER BY {order}")
        return f"{func.upper()}({', '.join(args)}) OVER ({' '.join(clauses)})"

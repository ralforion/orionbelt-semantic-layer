"""Locale-aware value formatting shared by the UI and the REST API.

Both surfaces apply the same column ``format`` patterns (e.g. ``#,##0.00``,
``0.00%``) and the same locale-driven separator rules. Keeping the logic in
one place avoids drift between what the UI shows and what the API returns
when ``format_values`` is requested.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = [
    "format_number",
    "format_row",
    "is_numeric_type_hint",
    "locale_separators",
    "parse_number_format",
    "to_tsv",
]


_COMMA_DECIMAL_LANGS = frozenset(
    {
        "de",
        "fr",
        "it",
        "es",
        "pt",
        "nl",
        "da",
        "nb",
        "nn",
        "sv",
        "fi",
        "pl",
        "cs",
        "sk",
        "hu",
        "ro",
        "bg",
        "hr",
        "sl",
        "sr",
        "tr",
        "el",
        "ru",
        "uk",
        "be",
        "ca",
        "id",
    }
)


def parse_number_format(fmt: str | None) -> tuple[bool, int, bool]:
    """Parse a display format pattern into ``(use_thousands, decimals, is_percent)``.

    Supported patterns: ``#,##0.00``, ``#,##0``, ``0.00%``, etc.
    Returns ``decimals = -1`` when no fractional digits are specified and
    the value should fall back to Python's default float representation.
    """
    if not fmt:
        return (False, -1, False)
    is_pct = fmt.endswith("%")
    body = fmt.rstrip("%").strip()
    use_thousands = "," in body
    decimals = -1
    if "." in body:
        after_dot = body.split(".")[-1]
        decimals = len(after_dot)
    elif is_pct:
        decimals = 0
    return (use_thousands, decimals, is_pct)


def locale_separators(locale: str) -> tuple[str, str]:
    """Return ``(thousands_sep, decimal_sep)`` for a BCP-47 locale tag.

    Empty / unknown locales fall back to the en-style ``"," / "."`` pair.
    """
    lang = locale.split("-")[0].lower() if locale else "en"
    if lang in _COMMA_DECIMAL_LANGS:
        return (".", ",")
    return (",", ".")


def format_number(val: int | float | Decimal, fmt: str | None, locale: str = "") -> str:
    """Format a numeric value using a display format pattern and locale.

    Accepts int / float / Decimal so callers don't have to pre-cast to
    ``float`` — that pre-cast was producing ``"52965.0"`` for integer
    columns with no format pattern, since ``str(float(52965))`` keeps the
    trailing ``.0``. Forwarding the original type preserves
    ``str(52965) == "52965"`` while the ``f"{:,.{n}f}"`` format string
    still works on all three types when a pattern is supplied.
    """
    use_thousands, decimals, is_pct = parse_number_format(fmt)
    if decimals < 0 and not use_thousands and not is_pct:
        return str(val)
    if is_pct:
        val = val * 100
    if decimals < 0:
        decimals = 0
    raw = f"{val:,.{decimals}f}" if use_thousands else f"{val:.{decimals}f}"
    tsep, dsep = locale_separators(locale)
    if tsep != "," or dsep != ".":
        raw = raw.replace(",", "\x00").replace(".", dsep).replace("\x00", tsep)
    return raw + ("%" if is_pct else "")


# Substrings that mark a column type hint as numeric. ``_build_type_map``
# can return either the high-level enum hint ("number", "int", "float") or
# the measure's raw ``data_type`` string ("decimal(18, 2)", "bigint", etc.),
# so the check has to be lexical rather than an equality test.
_NUMERIC_TYPE_TOKENS = (
    "number",
    "int",  # int, bigint, smallint, tinyint
    "float",
    "decimal",
    "numeric",
    "double",
    "real",
)


def is_numeric_type_hint(type_hint: str | None) -> bool:
    """Return True when *type_hint* designates a numeric column type.

    Accepts both the curated hints from ``_build_type_map`` ("number",
    "int", "float") and raw OBML / SQL ``data_type`` strings such as
    ``"decimal(18, 2)"`` or ``"bigint"``.
    """
    if not type_hint:
        return False
    h = type_hint.lower()
    return any(tok in h for tok in _NUMERIC_TYPE_TOKENS)


def format_row(
    row: list[object],
    column_names: list[str],
    fmt_map: dict[str, str | None],
    type_map: dict[str, str],
    locale: str = "",
) -> list[str | None]:
    """Apply UI-style formatting to a single row of result data.

    A cell is treated as numeric when **either** its column's type hint
    matches a numeric token (see :func:`is_numeric_type_hint`) **or** its
    Python value is an int / float / Decimal (booleans excluded). The
    Decimal branch matters in production: psycopg, snowflake-connector and
    several other drivers return ``decimal.Decimal`` for SQL ``NUMERIC`` /
    ``DECIMAL`` columns, which would otherwise fall through ``isinstance``
    against ``(int, float)`` and never reach ``format_number``.

    ``None`` cells pass through unchanged so the caller decides how to
    render missing values.
    """
    out: list[str | None] = []
    for cell, name in zip(row, column_names, strict=False):
        if cell is None:
            out.append(None)
            continue
        is_numeric_value = isinstance(cell, (int, float, Decimal)) and not isinstance(cell, bool)
        is_numeric = is_numeric_type_hint(type_map.get(name)) or is_numeric_value
        if is_numeric:
            try:
                # Preserve int / Decimal so unformatted integer columns stay
                # ``"52965"`` rather than ``"52965.0"``.
                out.append(format_number(cell, fmt_map.get(name), locale))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                out.append(str(cell))
        else:
            out.append(str(cell))
    return out


# ---------------------------------------------------------------------------
# TSV serialization
# ---------------------------------------------------------------------------

# RFC 4180-style quoting adapted for TSV: cells containing a tab, newline,
# carriage return, or double quote are wrapped in double quotes with internal
# double quotes doubled. Other cells are emitted as-is.
_TSV_SPECIAL = ("\t", "\n", "\r", '"')


def _quote_tsv_cell(value: str) -> str:
    if any(ch in value for ch in _TSV_SPECIAL):
        escaped = value.replace('"', '""')
        return f'"{escaped}"'
    return value


def to_tsv(columns: list[str], rows: list[list[str | None]], *, null: str = "") -> str:
    """Serialize a header + rows table to TSV text.

    ``rows`` should already be formatted strings (or ``None`` for missing).
    ``None`` cells render as ``null`` (default empty). Cells containing
    tab / newline / CR / double-quote are quoted.
    """
    lines: list[str] = []
    lines.append("\t".join(_quote_tsv_cell(c) for c in columns))
    for row in rows:
        lines.append("\t".join(_quote_tsv_cell(null if c is None else c) for c in row))
    return "\n".join(lines) + "\n"

"""OBML data type registry: parse, validate, and render abstract data types."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_DECIMAL_RE = re.compile(r"^decimal\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)$")

_SIMPLE_TYPES = frozenset(
    {"bigint", "integer", "double", "date", "timestamp", "time", "string", "boolean"}
)

MAX_PRECISION = 131072


@dataclass(frozen=True, slots=True)
class DecimalType:
    """A parametrized decimal type with precision and scale."""

    precision: int
    scale: int

    def render(self) -> str:
        return f"decimal({self.precision}, {self.scale})"

    def __str__(self) -> str:
        return self.render()


@dataclass(frozen=True, slots=True)
class SimpleType:
    """A non-parametrized type (bigint, integer, double, date, etc.)."""

    name: str

    def render(self) -> str:
        return self.name

    def __str__(self) -> str:
        return self.name


OBMLType = DecimalType | SimpleType


def decimal_width(precision: int, scale: int) -> tuple[int, int]:
    """A ``(precision, scale)`` with ``0 <= scale <= precision`` holding every
    value of ``DECIMAL(precision, scale)``.

    PostgreSQL accepts both a negative scale - ``NUMERIC(2, -3)`` rounds to
    thousands and holds up to 99000 - and a scale beyond the precision -
    ``NUMERIC(3, 5)`` holds up to 0.00999. Arrow and DuckDB accept neither, so
    the integer digits and the fractional digits are kept and the width is
    rebuilt from them: ``(5, 0)`` and ``(5, 5)``.
    """
    integer_digits = max(precision - scale, 0)
    scale = max(scale, 0)
    return max(integer_digits + scale, 1), scale


def default_as_decimal(value: object) -> Decimal | None:
    """A ``defaultValue`` as the number its SQL literal spells, or ``None``.

    An int or float is rendered as ``str(value)``, and text as a string the
    engine coerces; a boolean, or text that is not a finite number, is no
    number at all.
    """
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


BUILTIN_DEFAULT = DecimalType(precision=18, scale=2)
DIVISION_DEFAULT = DecimalType(precision=18, scale=6)
FLOAT_DEFAULT = SimpleType(name="double")


def parse_data_type(raw: str) -> OBMLType:
    """Parse a data_type string into a structured OBMLType.

    Raises ValueError for unrecognized or invalid types.
    """
    normalized = raw.strip().lower()

    if normalized in _SIMPLE_TYPES:
        return SimpleType(name=normalized)

    m = _DECIMAL_RE.match(normalized)
    if m:
        p, s = int(m.group(1)), int(m.group(2))
        if p <= 0:
            raise ValueError(f"decimal precision must be > 0, got {p}")
        if s < 0:
            raise ValueError(f"decimal scale must be >= 0, got {s}")
        if s > p:
            raise ValueError(f"decimal scale ({s}) cannot exceed precision ({p})")
        if p > MAX_PRECISION:
            raise ValueError(f"decimal precision ({p}) exceeds maximum ({MAX_PRECISION})")
        return DecimalType(precision=p, scale=s)

    raise ValueError(
        f"Unknown data_type '{raw}'. Supported: decimal(p, s), {', '.join(sorted(_SIMPLE_TYPES))}"
    )

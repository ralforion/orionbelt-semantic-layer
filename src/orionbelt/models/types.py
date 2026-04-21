"""OBML data type registry: parse, validate, and render abstract data types."""

from __future__ import annotations

import re
from dataclasses import dataclass

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

BUILTIN_DEFAULT = DecimalType(precision=18, scale=2)
DIVISION_DEFAULT = DecimalType(precision=18, scale=6)


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


def is_numeric_type(t: OBMLType) -> bool:
    """Return True if the type represents a numeric value."""
    if isinstance(t, DecimalType):
        return True
    return t.name in ("bigint", "integer", "double")

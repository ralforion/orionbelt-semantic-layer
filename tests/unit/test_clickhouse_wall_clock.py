"""A declared ``timestamp`` is a wall clock, and this engine has none.

ClickHouse's ``DateTime`` is an instant that renders against the server's
timezone, so the same stored data answered ``timestamp[ms, tz=Europe/Berlin]``
on one deployment and UTC on another. OBML's two timestamp types are a wall
clock and an instant, and the first has to survive the trip.

The conversion is in the SQL rather than in the result, because the compiled
statement is what a person can take away and run themselves.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import Cast, ColumnRef, Literal
from orionbelt.dialect.clickhouse import ClickHouseDialect
from orionbelt.models.types import parse_data_type


@pytest.fixture
def dialect() -> ClickHouseDialect:
    return ClickHouseDialect()


def test_the_cast_reads_the_value_as_text(dialect: ClickHouseDialect) -> None:
    """``toString`` renders the wall clock the engine shows; UTC labels it.

    Casting to ``DateTime64(3, 'UTC')`` directly would preserve the *instant*
    instead - measured, 13:45 on a Berlin server answers 11:45 that way, which
    is what ``timestamp_tz`` promises and this type does not.
    """
    cast = dialect.cast_to_obml_type(ColumnRef(name="Stamp"), parse_data_type("timestamp"))
    assert dialect.compile_expr(cast) == (
        "CAST(toDateTime64(toString(\"Stamp\"), 3, 'UTC') AS Nullable(DateTime64(3, 'UTC')))"
    )


def test_the_declared_type_carries_the_zone(dialect: ClickHouseDialect) -> None:
    """A bare ``DateTime64(3)`` would resolve against the server's timezone."""
    assert dialect.render_obml_type(parse_data_type("timestamp")) == "DateTime64(3, 'UTC')"


def test_a_null_pad_is_left_alone(dialect: ClickHouseDialect) -> None:
    """Every CFL union leg carries one, and ``toString(NULL)`` types as binary.

    The same exemption the decimal pre-round takes, for the same reason: there
    is no wall clock in a NULL to preserve.
    """
    cast = dialect.cast_to_obml_type(Literal.null(), parse_data_type("timestamp"))
    assert dialect.compile_expr(cast) == "CAST(NULL AS Nullable(DateTime64(3, 'UTC')))"


def test_a_date_target_is_untouched(dialect: ClickHouseDialect) -> None:
    """The rewrite is scoped to the type that has a wall clock to lose."""
    cast = dialect.cast_to_obml_type(ColumnRef(name="Day"), parse_data_type("date"))
    assert dialect.compile_expr(cast) == 'CAST("Day" AS Nullable(Date))'


def test_a_concrete_datetime_target_is_untouched(dialect: ClickHouseDialect) -> None:
    """A hand-built cast to the engine's own type says what it says.

    The rewrite belongs to OBML's ``timestamp``, which this dialect renders with
    the UTC label; a ``Cast`` naming ``DateTime64(3)`` is asking for the
    server's rendering and gets it.
    """
    sql = dialect.compile_expr(Cast(expr=ColumnRef(name="Stamp"), type_name="DateTime64(3)"))
    assert sql == 'CAST("Stamp" AS Nullable(DateTime64(3)))'

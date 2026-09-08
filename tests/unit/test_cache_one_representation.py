"""One cache entry means the same thing to every surface that reads it.

REST, pgwire and Flight share a cache: same key, same blob (KEY_VERSION). They
did not share a *representation*. REST and pgwire encoded their already
serialised rows, so a timestamp column was stored as an ISO ``string``, a date
as a ``string`` and binary as base64; Flight stored its Arrow table verbatim,
so the same three columns were ``timestamp[us]``, ``date32`` and ``binary``.
Whichever surface executed the query first decided what the others read, and
each read path only understood its own writer's convention.

These pin the fix: the driver's table is what gets stored, and a hit is
serialised by the same function a miss is.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

pa = pytest.importorskip("pyarrow", reason="pyarrow required for the result cache")

from orionbelt.api.query_cache import execution_result_from_data  # noqa: E402
from orionbelt.cache.result_codec import decode_data, encode_table  # noqa: E402
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult  # noqa: E402


def _warehouse_table() -> Any:
    """A result with one column of every type the two writers disagreed about."""
    return pa.table(
        {
            "Orders": pa.array([1, None], pa.int64()),
            "Amount": pa.array([Decimal("1.50"), None], pa.decimal128(18, 2)),
            "Ordered At": pa.array([dt.datetime(2024, 1, 2, 3, 4, 5), None], pa.timestamp("us")),
            "Shipped On": pa.array([dt.date(2024, 1, 2), None], pa.date32()),
            "Payload": pa.array([b"\x00\x01", None], pa.binary()),
            "Unsold": pa.array([None, None], pa.int64()),
        }
    )


def _miss(table: Any) -> ExecutionResult:
    """What the executor hands the response builder on a cache miss."""
    return ExecutionResult(
        columns=[ColumnMeta(name=f.name, type_hint="string") for f in table.schema],
        arrow_table=table,
        row_count=table.num_rows,
    )


def _hit(table: Any) -> ExecutionResult:
    """The same result, written to the cache and read back."""
    return execution_result_from_data(decode_data(encode_table(table)), execution_time_ms=1.0)


class TestTheStoredEntry:
    def test_the_blob_holds_the_warehouse_types(self) -> None:
        stored = decode_data(encode_table(_warehouse_table()))
        assert stored.schema.field("Ordered At").type == pa.timestamp("us")
        assert stored.schema.field("Shipped On").type == pa.date32()
        assert stored.schema.field("Payload").type == pa.binary()
        assert stored.schema.field("Amount").type == pa.decimal128(18, 2)
        assert stored.schema.field("Unsold").type == pa.int64()

    def test_flight_reads_what_rest_wrote(self) -> None:
        """Flight streams the decoded table straight to the client, so the
        stored types *are* the wire types. A REST-written entry used to hand
        it ``string`` where its own advertised schema said ``timestamp``.
        """
        original = _warehouse_table()
        streamed = decode_data(encode_table(original))
        assert streamed.schema == original.schema


class TestAHitEqualsItsMiss:
    """The invariant worth the KEY_VERSION bump."""

    def test_every_cell_matches(self) -> None:
        table = _warehouse_table()
        assert _hit(table).rows == _miss(table).rows

    def test_and_the_values_are_the_serialised_ones(self) -> None:
        """Not an accident of both paths being equally wrong."""
        rows = _hit(_warehouse_table()).rows
        assert rows[0] == [1, Decimal("1.50"), "2024-01-02T03:04:05", "2024-01-02", "AAE=", None]

    def test_an_adbc_opaque_numeric_matches_too(self) -> None:
        """PostgreSQL NUMERIC under ADBC: a string in Arrow, a Decimal in a row.

        The executor parses it on the way out, so a hit only agrees with its
        miss if the hit goes through the executor - which is what holding the
        table, rather than pre-serialising it, buys.
        """
        opaque = pa.opaque(pa.string(), "numeric", "PostgreSQL")
        table = pa.table({"Amount": pa.array(["1.50", None], pa.string()).cast(opaque)})
        assert _hit(table).rows == _miss(table).rows == [[Decimal("1.50")], [None]]

    def test_an_interval_matches_too(self) -> None:
        """The normalisation that used to live on one side only.

        ``_arrow_to_rows`` turns a duration into ``str(timedelta)``. That was
        correct for a miss and irrelevant for a hit, because the stored cell
        was already a string. Now both go through it.
        """
        table = pa.table({"Age": pa.array([dt.timedelta(days=1), None], pa.duration("us"))})
        assert _hit(table).rows == _miss(table).rows == [["1 day, 0:00:00"], [None]]


class TestReconciliationOnAHit:
    def test_a_hit_can_be_reconciled(self) -> None:
        """It is table-backed, so the cast has something to work on.

        A row-backed hit made ``reconcile_to_declared`` a silent no-op, which
        is the trap four read paths rediscovered one at a time.
        """
        table = pa.table({"flag": pa.array([0, 1], pa.int64())})
        hit = _hit(table)
        assert hit.reconcile_to_declared({"flag": pa.bool_()}) == []
        assert hit.arrow_table.schema.field("flag").type == pa.bool_()

    def test_and_it_names_what_it_could_not_cast(self) -> None:
        table = pa.table({"flag": pa.array([0, 1, 7], pa.int64())})
        skips = _hit(table).reconcile_to_declared({"flag": pa.bool_()})
        assert [name for name, _ in skips] == ["flag"]

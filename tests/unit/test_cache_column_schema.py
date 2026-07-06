"""The result column schema is cached alongside the row data (entry sidecar).

Covers two review findings on the data-only cache:
  1. An empty / all-null result must keep its column types + formats on a hit.
     The Arrow blob infers types from values, so an empty numeric column becomes
     Arrow ``null`` and would fall back to string/no-format without the sidecar.
  3. Accept negotiation must recognize the frame media type the endpoint emits.
"""

from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow", reason="pyarrow required for the data codec")

from orionbelt.api.query_cache import execution_result_from_data  # noqa: E402
from orionbelt.api.services.query_execution import (  # noqa: E402
    ARROW_STREAM_MEDIA_TYPE,
    ORIONBELT_RESULT_MEDIA_TYPE,
    negotiate_execute_format,
)
from orionbelt.cache import result_codec  # noqa: E402


def test_sidecar_preserves_types_for_empty_result() -> None:
    # 0 rows -> pyarrow infers the column as null-typed.
    table = result_codec.build_result_table(["Amount"], [])
    assert pa.types.is_null(table.schema.field("Amount").type)

    # Without the sidecar, arrow inference cannot recover "number"/"#,##0.00".
    inferred = execution_result_from_data(table, execution_time_ms=1.0)
    assert inferred.columns[0].type_hint != "number"

    # With the sidecar (what a fresh miss produced), the exact type + format
    # survive the empty result.
    sidecar = [{"name": "Amount", "type": "number", "format": "#,##0.00"}]
    kept = execution_result_from_data(table, execution_time_ms=1.0, columns=sidecar)
    assert kept.columns[0].type_hint == "number"
    assert kept.columns[0].default_format == "#,##0.00"


def test_sidecar_preserves_types_for_all_null_rows() -> None:
    table = result_codec.build_result_table(["Amount"], [[None], [None]])
    sidecar = [{"name": "Amount", "type": "number", "format": "#,##0.00"}]
    kept = execution_result_from_data(table, execution_time_ms=1.0, columns=sidecar)
    assert kept.columns[0].type_hint == "number"
    assert kept.columns[0].default_format == "#,##0.00"
    assert kept.row_count == 2


def test_negotiate_accepts_result_frame_media_type() -> None:
    # The endpoint emits ORIONBELT_RESULT_MEDIA_TYPE, so a client asking for it
    # must get the arrow frame (regression: only the legacy token was matched).
    assert negotiate_execute_format("json", ORIONBELT_RESULT_MEDIA_TYPE) == "arrow"
    # The historical arrow-stream token still negotiates to arrow.
    assert negotiate_execute_format("json", ARROW_STREAM_MEDIA_TYPE) == "arrow"
    # Anything else stays JSON.
    assert negotiate_execute_format("json", "application/json") == "json"
    assert negotiate_execute_format("json", None) == "json"
    # An explicit ?format= still wins over the Accept header.
    assert negotiate_execute_format("tsv", ORIONBELT_RESULT_MEDIA_TYPE) == "tsv"

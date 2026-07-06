"""The UI fetches results as the length-prefixed Arrow frame; verify its decoder
reconstructs the same dict shape the JSON path yields (rows + fresh envelope)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pyarrow", reason="pyarrow required for arrow transport")
pytest.importorskip("gradio", reason="gradio required to import UI handlers")

from orionbelt.cache import result_codec  # noqa: E402
from orionbelt.ui.handlers import _decode_arrow_execute_response  # noqa: E402


class _FakeResp:
    """Stand-in for an httpx response carrying the raw result frame."""

    def __init__(self, content: bytes) -> None:
        self.content = content


def _frame(meta: dict, column_names: list[str], rows: list[list]) -> bytes:
    """Build the ``[u32 len][json meta][gzip'd arrow data]`` wire frame."""
    gzipped = result_codec.encode_data(column_names, rows)
    meta_bytes = json.dumps(meta).encode("utf-8")
    return len(meta_bytes).to_bytes(4, "big") + meta_bytes + gzipped


def test_ui_decodes_arrow_into_json_shaped_dict() -> None:
    meta = {
        "columns": [
            {"name": "Country", "type": "string", "format": None},
            {"name": "Revenue", "type": "decimal(18, 2)", "format": "#,##0.00"},
        ],
        "sql": "SELECT country, SUM(revenue) FROM sales GROUP BY country",
        "dialect": "duckdb",
        "explain": None,
        "warnings": [],
        "sql_valid": True,
        "execution_time_ms": 3.0,
        "timezone": "UTC",
        "resolved": {"fact_tables": ["SALES"], "dimensions": ["Country"], "measures": ["Revenue"]},
        "physical_tables": ["WH.PUBLIC.SALES"],
        "row_count": 2,
        "cached": True,
        "cached_at": "2026-07-02T00:00:00Z",
    }
    resp = _FakeResp(_frame(meta, ["Country", "Revenue"], [["US", 100.0], ["UK", 200.0]]))

    data = _decode_arrow_execute_response(resp)

    assert data["sql"].startswith("SELECT country")
    assert data["dialect"] == "duckdb"
    assert [c["name"] for c in data["columns"]] == ["Country", "Revenue"]
    assert data["rows"] == [["US", 100.0], ["UK", 200.0]]
    assert data["row_count"] == 2
    assert data["timezone"] == "UTC"
    # Cache indicator survives the Arrow transport (drives the UI "(cache)" tag).
    assert data["cached"] is True
    assert data["cached_at"] == "2026-07-02T00:00:00Z"

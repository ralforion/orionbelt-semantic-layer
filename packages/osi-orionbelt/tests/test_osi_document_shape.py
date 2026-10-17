"""One semantic model per document, its properties at the document root.

Apache Ossie replaced the ``semantic_model`` array with a single model at the
root (apache/ossie#383) at the same ``0.2.0.dev0`` version. The export writes
the current shape; the import still reads the earlier one, with a warning, so
documents exported before osi-orionbelt 0.5.0 keep converting.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

import osi_orionbelt.converter as conv

_OBML: dict[str, Any] = {
    "version": 1.0,
    "dataObjects": {
        "Orders": {
            "code": "orders",
            "database": "WH",
            "schema": "PUBLIC",
            "columns": {
                "Order ID": {"code": "order_id", "abstractType": "string", "primaryKey": True},
                "Amount": {"code": "amount", "abstractType": "float"},
            },
        }
    },
    "measures": {
        "Revenue": {
            "columns": [{"dataObject": "Orders", "column": "Amount"}],
            "resultType": "float",
            "aggregation": "sum",
        }
    },
}


def _wrapped(flat: dict[str, Any]) -> dict[str, Any]:
    model = {k: v for k, v in flat.items() if k != "version"}
    return {"version": flat["version"], "semantic_model": [model]}


class TestExport:
    def test_model_properties_are_at_the_root(self) -> None:
        osi = conv.OBMLtoOSI(_OBML, "sales").convert()
        assert "semantic_model" not in osi
        assert osi["name"] == "sales"
        assert [d["name"] for d in osi["datasets"]] == ["Orders"]
        assert [m["name"] for m in osi["metrics"]] == ["Revenue"]

    def test_export_validates_against_the_current_schema(self) -> None:
        pytest.importorskip("jsonschema")
        osi = conv.OBMLtoOSI(_OBML, "sales").convert()
        result = conv.validate_osi(osi)
        assert result.schema_errors == []
        assert result.semantic_errors == []


class TestLegacyWrapperImport:
    def test_wrapped_document_converts_like_the_flat_one(self) -> None:
        flat = conv.OBMLtoOSI(_OBML, "sales").convert()
        from_flat = conv.OSItoOBML(copy.deepcopy(flat)).convert()
        importer = conv.OSItoOBML(_wrapped(flat))
        assert importer.convert() == from_flat
        assert any("semantic_model" in w and "#383" in w for w in importer.warnings)

    def test_flat_document_has_no_wrapper_warning(self) -> None:
        importer = conv.OSItoOBML(conv.OBMLtoOSI(_OBML, "sales").convert())
        importer.convert()
        assert not any("semantic_model" in w for w in importer.warnings)

    def test_wrapper_is_a_schema_error_but_still_checked(self) -> None:
        pytest.importorskip("jsonschema")
        flat = conv.OBMLtoOSI(_OBML, "sales").convert()
        flat["datasets"].append(copy.deepcopy(flat["datasets"][0]))
        result = conv.validate_osi(_wrapped(flat))
        assert any("semantic_model" in e for e in result.schema_errors)
        assert any("DUPLICATE_DATASET" in e for e in result.semantic_errors)


class TestValidatorOnFlatDocuments:
    def test_duplicate_dataset_is_reported(self) -> None:
        pytest.importorskip("jsonschema")
        osi = conv.OBMLtoOSI(_OBML, "sales").convert()
        osi["datasets"].append(copy.deepcopy(osi["datasets"][0]))
        result = conv.validate_osi(osi)
        assert any("DUPLICATE_DATASET" in e for e in result.semantic_errors)

    def test_relationship_to_unknown_dataset_is_reported(self) -> None:
        pytest.importorskip("jsonschema")
        osi = conv.OBMLtoOSI(_OBML, "sales").convert()
        osi["relationships"] = [
            {
                "name": "orders_to_nowhere",
                "from": "Orders",
                "to": "Nowhere",
                "from_columns": ["Order ID"],
                "to_columns": ["id"],
            }
        ]
        result = conv.validate_osi(osi)
        assert any("UNKNOWN_DATASET_REF" in e and "Nowhere" in e for e in result.semantic_errors)

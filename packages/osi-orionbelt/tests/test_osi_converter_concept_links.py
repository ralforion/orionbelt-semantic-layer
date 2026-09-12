"""OBML external concept mappings survive the OBML -> OSI -> OBML round trip.

OSI has no slot for ``ontology.prefixes`` or ``externalConceptMappings``, so
both ride in the OBSL-vendor ``custom_extensions`` of the entity each OBML
object becomes (semantic model, dataset, field, metric) and are restored
verbatim. A mapping that only makes sense with its prefix must come back
with that prefix, or the OBML parser would reject it as
``UNKNOWN_ONTOLOGY_PREFIX``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import osi_orionbelt.converter as conv

CORP = "https://ontology.example.com/business/"

_LINK = {"concept": "corp:Thing", "relation": "exact"}
_RICH_LINK = {
    "concept": "https://schema.org/MonetaryAmount",
    "relation": "broader",
    "justification": "curated",
    "source": "enterprise-finance-ontology",
    "ontologyVersion": "2026.1",
    "confidence": 0.9,
    "comment": "Approved by Finance",
}

_OBML: dict[str, Any] = {
    "version": 1.0,
    "ontology": {"prefixes": {"corp": CORP}},
    "externalConceptMappings": [{"concept": "corp:SalesModel", "relation": "exact"}],
    "dataObjects": {
        "Orders": {
            "code": "ORDERS",
            "database": "W",
            "schema": "P",
            "externalConceptMappings": [{"concept": "corp:Order", "relation": "exact"}],
            "columns": {
                "Order ID": {"code": "ORDER_ID", "abstractType": "string"},
                "Amount": {"code": "AMOUNT", "abstractType": "float"},
                "Order Date": {"code": "ORDER_DATE", "abstractType": "date"},
            },
        },
    },
    "dimensions": {
        "Order ID": {
            "dataObject": "Orders",
            "column": "Order ID",
            "externalConceptMappings": [{"concept": "corp:OrderIdentifier", "relation": "exact"}],
        },
        "Order Month": {
            "dataObject": "Orders",
            "column": "Order Date",
            "resultType": "date",
            "timeGrain": "month",
            "externalConceptMappings": [{"concept": "corp:Month", "relation": "close"}],
        },
        "Order Year": {
            "dataObject": "Orders",
            "column": "Order Date",
            "resultType": "date",
            "timeGrain": "year",
            "externalConceptMappings": [{"concept": "corp:Year", "relation": "close"}],
        },
    },
    "measures": {
        "Revenue": {
            "aggregation": "sum",
            "columns": [{"dataObject": "Orders", "column": "Amount"}],
            "owner": "finance",
            "externalConceptMappings": [
                _RICH_LINK,
                {"concept": "corp:NetRevenue", "relation": "exact"},
            ],
        },
        "Delegated": {
            "aggregation": "measure",
            "externalConceptMappings": [_LINK],
        },
    },
    "metrics": {
        "Revenue Doubled": {
            "expression": "{[Revenue]} * 2",
            "externalConceptMappings": [{"concept": "corp:DoubleRevenue", "relation": "narrower"}],
        },
        "Running Revenue": {
            "type": "cumulative",
            "measure": "Revenue",
            "timeDimension": "Order Month",
            "externalConceptMappings": [{"concept": "corp:RunningRevenue", "relation": "related"}],
        },
        "Revenue YoY": {
            "type": "period_over_period",
            "expression": "{[Revenue]}",
            "periodOverPeriod": {
                "timeDimension": "Order Month",
                "offset": 1,
                "offsetGrain": "year",
            },
            "externalConceptMappings": [{"concept": "corp:YoY", "relation": "related"}],
        },
        "Revenue Rank": {
            "type": "window",
            "measure": "Revenue",
            "windowFunction": "rank",
            "externalConceptMappings": [{"concept": "corp:Rank", "relation": "related"}],
        },
    },
}


def _roundtrip(obml: dict) -> dict:
    osi = conv.OBMLtoOSI(copy.deepcopy(obml)).convert()
    return conv.OSItoOBML(osi).convert()


def _links(obj: dict) -> list:
    return obj.get("externalConceptMappings", [])


class TestRoundTrip:
    def test_model_level_ontology_and_mappings(self) -> None:
        back = _roundtrip(_OBML)
        assert back["ontology"] == {"prefixes": {"corp": CORP}}
        assert _links(back) == _OBML["externalConceptMappings"]

    def test_data_object(self) -> None:
        back = _roundtrip(_OBML)
        assert _links(back["dataObjects"]["Orders"]) == [
            {"concept": "corp:Order", "relation": "exact"}
        ]

    def test_primary_and_extra_dimensions(self) -> None:
        back = _roundtrip(_OBML)
        dims = back["dimensions"]
        assert _links(dims["Order ID"]) == [
            {"concept": "corp:OrderIdentifier", "relation": "exact"}
        ]
        # Two dimensions over one column: the first is carried on the field,
        # the second as an extra descriptor. Both keep their links.
        assert _links(dims["Order Month"]) == [{"concept": "corp:Month", "relation": "close"}]
        assert _links(dims["Order Year"]) == [{"concept": "corp:Year", "relation": "close"}]

    def test_measures_keep_rich_metadata(self) -> None:
        back = _roundtrip(_OBML)
        assert (
            _links(back["measures"]["Revenue"])
            == _OBML["measures"]["Revenue"]["externalConceptMappings"]
        )
        assert back["measures"]["Revenue"]["owner"] == "finance"
        assert _links(back["measures"]["Delegated"]) == [_LINK]

    def test_every_metric_type(self) -> None:
        back = _roundtrip(_OBML)
        for name in ("Revenue Doubled", "Running Revenue", "Revenue YoY", "Revenue Rank"):
            assert (
                _links(back["metrics"][name]) == _OBML["metrics"][name]["externalConceptMappings"]
            ), name

    def test_no_mappings_means_no_key(self) -> None:
        plain = copy.deepcopy(_OBML)
        plain.pop("ontology")
        plain.pop("externalConceptMappings")
        for section in ("dataObjects", "dimensions", "measures", "metrics"):
            for obj in plain[section].values():
                obj.pop("externalConceptMappings", None)
        back = _roundtrip(plain)
        assert "ontology" not in back
        assert "externalConceptMappings" not in back
        for section in ("dataObjects", "dimensions", "measures", "metrics"):
            for obj in back[section].values():
                assert "externalConceptMappings" not in obj

    def test_links_ride_in_the_obsl_vendor_extension(self) -> None:
        osi = conv.OBMLtoOSI(copy.deepcopy(_OBML)).convert()
        metric = next(m for m in osi["semantic_model"][0]["metrics"] if m["name"] == "Revenue")
        obsl_exts = [e for e in metric["custom_extensions"] if e["vendor_name"] == "ORIONBELT"]
        assert len(obsl_exts) == 1, "links merge into the single ORIONBELT payload"
        assert (
            json.loads(obsl_exts[0]["data"])["obml_external_concept_mappings"]
            == _OBML["measures"]["Revenue"]["externalConceptMappings"]
        )

    def test_foreign_garbage_under_the_key_is_ignored(self) -> None:
        osi = conv.OBMLtoOSI(copy.deepcopy(_OBML)).convert()
        model = osi["semantic_model"][0]
        ext = next(e for e in model["custom_extensions"] if e["vendor_name"] == "ORIONBELT")
        data = json.loads(ext["data"])
        data["obml_external_concept_mappings"] = "not a list"
        data["obml_ontology"] = ["not a mapping"]
        ext["data"] = json.dumps(data)
        back = conv.OSItoOBML(osi).convert()
        assert "externalConceptMappings" not in back
        assert "ontology" not in back

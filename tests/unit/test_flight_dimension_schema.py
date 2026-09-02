"""The advertised schema names the columns the stream actually carries.

``semantic_result_schema`` reads each ``select.dimensions`` entry, and an entry
is not always its own label: ``"At:day"`` is a grain request the compiler
projects ``AS "At"``, and a coalesce entry names its output in ``as`` while its
type lives on the dimensions it combines. Reading the entry as both label and
lookup key advertised ``At:day: string`` beside a stream carrying a timestamp
called ``At`` - a mismatch a strict Flight client validates.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

pytest.importorskip("ob_flight", reason="ob-flight-extension not installed")

from ob_flight.server_execution import semantic_result_schema  # noqa: E402

from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.models.semantic import (  # noqa: E402
    DataObject,
    DataObjectColumn,
    DataType,
    Dimension,
    SemanticModel,
)


def _model() -> SemanticModel:
    model = SemanticModel(
        data_objects={
            "Events": DataObject(
                name="Events",
                code="events",
                database="db",
                schema_name="public",
                columns={
                    "Stamp": DataObjectColumn(
                        name="Stamp", code="stamp", abstract_type=DataType.TIMESTAMP
                    )
                },
            )
        }
    )
    for name in ("At", "Ordered At", "Shipped At"):
        model.dimensions[name] = Dimension(
            name=name, view="Events", column="Stamp", result_type=DataType.TIMESTAMP
        )
    return model


def _schema(select: dict[str, object], **query: object) -> pa.Schema:
    payload: dict[str, object] = {"select": select, **query}
    return semantic_result_schema(None, QueryObject.model_validate(payload), _model())


def test_a_grain_request_is_named_and_typed_by_its_dimension() -> None:
    """The compiler projects ``AS "At"``, so the schema says ``At``."""
    schema = _schema({"dimensions": ["At:day"], "measures": []})
    assert schema.names == ["At"]
    assert schema.field(0).type == pa.timestamp("us")


def test_a_plain_dimension_still_resolves() -> None:
    schema = _schema({"dimensions": ["At"], "measures": []})
    assert schema.names == ["At"]
    assert schema.field(0).type == pa.timestamp("us")


def test_a_coalesce_takes_its_type_from_its_members() -> None:
    """The alias names the column; the members say what is in it."""
    schema = _schema(
        {"dimensions": [{"coalesce": ["Ordered At", "Shipped At"], "as": "When"}], "measures": []}
    )
    assert schema.names == ["When"]
    assert schema.field(0).type == pa.timestamp("us")


def test_an_unknown_dimension_is_still_a_string() -> None:
    """Nothing declared, nothing claimed - the fallback is unchanged."""
    schema = _schema({"dimensions": ["Nowhere"], "measures": []})
    assert schema.names == ["Nowhere"]
    assert schema.field(0).type == pa.utf8()


def test_the_grouping_flags_follow_the_same_labels() -> None:
    """A ROLLUP adds one flag per dimension, named after the column it flags."""
    schema = _schema({"dimensions": ["At:day"], "measures": []}, grouping="rollup")
    assert "_g_At" in schema.names
    assert "_g_At:day" not in schema.names

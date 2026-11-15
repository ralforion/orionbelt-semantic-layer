# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Offline guard on the vendored Apache Ossie core-spec schema.

Documents the structural surface the converter depends on and fails loudly if a
schema sync removes it - a conscious-review gate that complements the networked
weekly drift workflow (``.github/workflows/ossie-schema-drift.yml``). Also
exercises the drift-diff logic without touching the network.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

_PKG_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA = _PKG_ROOT / "src" / "osi_orionbelt" / "schemas" / "osi-schema.json"
_DRIFT_SCRIPT = _PKG_ROOT / "scripts" / "check_ossie_schema_drift.py"


def _load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA.read_text())


def _defs(schema: dict[str, Any]) -> dict[str, Any]:
    return schema.get("$defs") or schema.get("definitions") or {}


def _drift_module() -> Any:
    spec = importlib.util.spec_from_file_location("_ossie_drift", _DRIFT_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestVendoredSchemaContract:
    """The converter relies on these; a sync that drops them must fail here."""

    def test_datatype_enum_present_and_complete(self) -> None:
        dt = _defs(_load_schema()).get("DataType")
        assert dt is not None, "upstream added DataType; vendored copy must carry it"
        assert set(dt["enum"]) == {
            "String",
            "Integer",
            "Decimal",
            "Float",
            "Boolean",
            "Date",
            "Time",
            "DateTime",
            "DateTimeTz",
            "Opaque",
        }

    def test_field_and_metric_carry_datatype(self) -> None:
        defs = _defs(_load_schema())
        assert "datatype" in defs["Field"]["properties"]
        assert "datatype" in defs["Metric"]["properties"]

    def test_dataset_carries_keys_for_cardinality_inference(self) -> None:
        props = _defs(_load_schema())["Dataset"]["properties"]
        assert "primary_key" in props
        assert "unique_keys" in props

    def test_version_const_pinned(self) -> None:
        # The schema hard-pins the document version. When Ossie cuts a real
        # 0.2.0 (dropping .dev0), this flips - and the converter's pinned version
        # + fixtures will need updating, so it must be caught.
        version = _load_schema().get("properties", {}).get("version", {}).get("const")
        assert version == "0.2.0.dev0"

    def test_relationship_still_has_no_cardinality(self) -> None:
        # If upstream ever adds a cardinality field, cardinality inference from
        # unique_keys should defer to it - this test flags that change.
        props = set(_defs(_load_schema())["Relationship"]["properties"])
        assert props == {
            "name",
            "from",
            "to",
            "from_columns",
            "to_columns",
            "ai_context",
            "custom_extensions",
        }


class TestDriftDiff:
    """The drift-diff logic reports nothing for identical schemas, and reports
    exactly the delta for a mutated one."""

    def test_identical_is_in_sync(self) -> None:
        schema = _load_schema()
        assert _drift_module().diff(schema, schema) == []

    def test_added_property_is_reported(self) -> None:
        mod = _drift_module()
        base = _load_schema()
        mutated = json.loads(json.dumps(base))
        _defs(mutated)["Relationship"]["properties"]["cardinality"] = {"type": "string"}
        lines = mod.diff(base, mutated)
        assert any("cardinality" in line and "property added upstream" in line for line in lines)

    def test_version_flip_is_reported(self) -> None:
        # A .dev0 -> released 0.2.0 flip is drift even if $defs are identical.
        mod = _drift_module()
        base = _load_schema()
        mutated = json.loads(json.dumps(base))
        mutated["properties"]["version"]["const"] = "0.2.0"
        lines = mod.diff(base, mutated)
        assert any("version` const changed" in line for line in lines)
        # Sanity: without the version change, identical schemas are in sync.
        assert mod.diff(base, base) == []


class _Response:
    """Stands in for the ``urlopen`` context manager."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TestDriftExitCodes:
    """A broken check must fail the run; only a transient fetch failure may skip.

    Upstream renamed ``osi-schema.json`` to ``ossie-schema.json``; the check got a
    404, printed "skipping" and exited 0 for weeks, which read as "in sync".
    """

    def _run(self, monkeypatch: Any, outcome: Any) -> int:
        mod = _drift_module()

        def fake_urlopen(url: str, timeout: int) -> _Response:
            if isinstance(outcome, Exception):
                raise outcome
            return _Response(outcome)

        monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(mod.sys, "argv", ["check_ossie_schema_drift.py"])
        return int(mod.main())

    def test_points_at_the_current_upstream_file(self) -> None:
        assert _drift_module().UPSTREAM_URL.endswith("/core-spec/ossie-schema.json")

    def test_in_sync_exits_zero(self, monkeypatch: Any) -> None:
        assert self._run(monkeypatch, _SCHEMA.read_bytes()) == 0

    def test_drift_exits_one(self, monkeypatch: Any) -> None:
        mutated = _load_schema()
        mutated["properties"]["version"]["const"] = "9.9.9"
        assert self._run(monkeypatch, json.dumps(mutated).encode()) == 1

    def test_missing_upstream_file_exits_two(self, monkeypatch: Any) -> None:
        gone = urllib.error.HTTPError("u", 404, "Not Found", Message(), None)
        assert self._run(monkeypatch, gone) == 2

    def test_non_json_answer_exits_two(self, monkeypatch: Any) -> None:
        assert self._run(monkeypatch, b"<html>moved</html>") == 2

    def test_network_failure_skips(self, monkeypatch: Any) -> None:
        assert self._run(monkeypatch, urllib.error.URLError("no route to host")) == 0

    def test_server_error_skips(self, monkeypatch: Any) -> None:
        busy = urllib.error.HTTPError("u", 503, "Unavailable", Message(), None)
        assert self._run(monkeypatch, busy) == 0

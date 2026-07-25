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
"""Detect drift between the vendored Ossie core-spec schema and upstream.

Fetches the current Apache Ossie ``core-spec/osi-schema.json`` and compares its
structural surface (``$defs`` names, per-def properties + ``required`` + enum
members) against the copy vendored in this package. Prints a Markdown report and
exits non-zero when they differ, so a scheduled CI job can open an issue.

Usage::

    python check_ossie_schema_drift.py           # fetch upstream, diff, exit 1 on drift
    python check_ossie_schema_drift.py --help
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

UPSTREAM_URL = "https://raw.githubusercontent.com/apache/ossie/main/core-spec/osi-schema.json"
VENDORED = (
    Path(__file__).resolve().parent.parent / "src" / "osi_orionbelt" / "schemas" / "osi-schema.json"
)


def _defs(schema: dict[str, Any]) -> dict[str, Any]:
    return schema.get("$defs") or schema.get("definitions") or {}


def _fingerprint(schema: dict[str, Any]) -> dict[str, Any]:
    """Structural surface we depend on: per-def props, required, enum."""
    out: dict[str, Any] = {}
    for name, spec in _defs(schema).items():
        out[name] = {
            "properties": sorted((spec.get("properties") or {}).keys()),
            "required": sorted(spec.get("required") or []),
            "enum": spec.get("enum"),
        }
    return out


def diff(vendored: dict[str, Any], upstream: dict[str, Any]) -> list[str]:
    """Return human-readable drift lines; empty list means in sync."""
    lines: list[str] = []
    v, u = _fingerprint(vendored), _fingerprint(upstream)

    for name in sorted(set(u) - set(v)):
        lines.append(f"- **def added upstream:** `{name}`")
    for name in sorted(set(v) - set(u)):
        lines.append(f"- **def removed upstream:** `{name}`")

    for name in sorted(set(v) & set(u)):
        vp, up = set(v[name]["properties"]), set(u[name]["properties"])
        for p in sorted(up - vp):
            lines.append(f"- `{name}`: **property added upstream** `{p}`")
        for p in sorted(vp - up):
            lines.append(f"- `{name}`: **property removed upstream** `{p}`")
        if v[name]["required"] != u[name]["required"]:
            lines.append(
                f"- `{name}`: **required changed** {v[name]['required']} -> {u[name]['required']}"
            )
        if v[name]["enum"] != u[name]["enum"]:
            lines.append(f"- `{name}`: **enum changed** {v[name]['enum']} -> {u[name]['enum']}")
    return lines


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0

    vendored = json.loads(VENDORED.read_text())
    try:
        with urllib.request.urlopen(UPSTREAM_URL, timeout=30) as resp:  # noqa: S310 (trusted host)
            upstream = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network/parse failure is not "drift"
        print(f"Could not fetch upstream schema ({exc}); skipping drift check.")
        return 0

    lines = diff(vendored, upstream)
    if not lines:
        print("Vendored Ossie schema is in sync with upstream `apache/ossie`.")
        return 0

    print("## Ossie schema drift detected\n")
    print(f"The vendored `osi-schema.json` differs from upstream ({UPSTREAM_URL}):\n")
    print("\n".join(lines))
    print(
        "\nUpdate the vendored copy and propagate any type/field changes through the converter "
        "and `test_osi_schema_freshness.py`."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

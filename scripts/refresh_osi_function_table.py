"""Write the OBML function table osi-orionbelt ships beside its OBML schema.

The converter cannot import OrionBelt, yet must not write a measure OBML's
validator refuses: a call to a catalog function with the wrong number of
arguments (``WRONG_FUNCTION_ARITY``) or a unit not in its list
(``UNKNOWN_TIME_UNIT``) stops the whole model loading. It reads this table.
``tests/unit/test_osi_orionbelt_schema_sync.py`` fails when the table drifts
from ``models/functions.py``::

    uv run python scripts/refresh_osi_function_table.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orionbelt.models.functions import FUNCTION_CATALOG

TABLE = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "osi-orionbelt"
    / "src"
    / "osi_orionbelt"
    / "schemas"
    / "obml-functions.json"
)


def function_table() -> dict[str, Any]:
    """Each catalog function's arity and, for a date/time function, its units."""
    table: dict[str, Any] = {}
    for name, spec in sorted(FUNCTION_CATALOG.items()):
        entry: dict[str, Any] = {"args": [spec.min_args, spec.max_args]}
        if spec.unit_argument is not None:
            entry["unit"] = spec.unit_argument
            entry["units"] = list(spec.units)
        table[name] = entry
    return table


def render(table: dict[str, Any]) -> str:
    """One function per line, so a catalog change is a one-line diff."""
    lines = [f"  {json.dumps(name)}: {json.dumps(entry)}" for name, entry in table.items()]
    return "{\n" + ",\n".join(lines) + "\n}\n"


if __name__ == "__main__":
    TABLE.write_text(render(function_table()))
    print(f"wrote {TABLE}")

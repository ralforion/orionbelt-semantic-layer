"""Run one statement on a Hardcharger worker and print the outcome as JSON.

Used by test_compose.sh from a container on the Compose project's network, the
position OBSL queries the workers from:

    python compose_probe.py WORKER USER PASSWORD SQL

Prints {"ok": true, "rows": [...]} or {"ok": false, "error": "..."}.
"""

from __future__ import annotations

import json
import sys

import adbc_driver_hardcharger.dbapi as hardcharger


def run(worker: str, user: str, password: str, sql: str) -> dict[str, object]:
    try:
        con = hardcharger.connect(
            uri=f"hardcharger://{worker}?tls=false",
            username=user,
            password=password,
            db_kwargs={"adbc.chargewire.session.reconnect": "false"},
        )
    except Exception as exc:  # noqa: BLE001 - report every failure as data
        return {"ok": False, "error": f"connect: {type(exc).__name__}: {exc}"}
    try:
        cur = con.cursor()
        cur.execute(sql)
        return {"ok": True, "rows": cur.fetch_arrow_table().to_pylist()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        con.close()


if __name__ == "__main__":
    print(json.dumps(run(*sys.argv[1:5]), default=str))

#!/usr/bin/env python
"""Wire up the Dremio side of the semantic-sidecar demo, then prove it.

Idempotent. Run after `docker compose up -d` once Dremio is healthy:

    uv run python demo/dremio/bootstrap.py

Steps:
  1. Bootstrap Dremio's first admin user (no-op on re-run).
  2. Register MinIO as an S3 source named ``lake`` (S3 compatibility mode).
  3. Promote every ``lake.commerce.<table>`` Parquet folder as a dataset.
  4. Register ONE Postgres source ``obsl`` -> the OrionBelt pgwire surface.
  5. Run the same business question two ways and print them side by side:
       * RAW    : hand-written GROUP BY over the Parquet datasets
       * GOVERNED: SELECT dim, measure FROM obsl.commerce.model  (federated
                   into OrionBelt, which compiles Dremio SQL and pushes it
                   back into Dremio via Arrow Flight)
"""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import quote

import httpx

DREMIO_REST = os.environ.get("DREMIO_REST_URL", "http://localhost:19047")
ADMIN_USER = "obsl_admin"
ADMIN_PASS = "obsl_admin_pw_123!"  # noqa: S105 - local demo container only

# S3/MinIO source
LAKE_SOURCE = "lake"
BUCKET = "commerce"
MINIO_ENDPOINT = "minio:9000"  # inside the compose network
MINIO_KEY = "minioadmin"
MINIO_SECRET = "minioadmin"  # noqa: S105 - local demo container only

# The 15 commerce tables promoted as datasets (folder == dataset name).
TABLES = [
    "acctbal",
    "banks",
    "calendar",
    "channels",
    "clientcomplaints",
    "clients",
    "countries",
    "employees",
    "products",
    "purchases",
    "regions",
    "returns",
    "sales",
    "shipments",
    "suppliers",
]

# Postgres source -> OrionBelt pgwire. ``databaseName`` is the OBML model
# name ("commerce"); the model surfaces as the single virtual table ``model``.
PG_SOURCE = "obsl"
PG_DATABASE = "commerce"
PG_HOST = "obsl"
PG_PORT = "5432"


def _login(client: httpx.Client) -> str:
    client.put(
        "/apiv2/bootstrap/firstuser",
        json={
            "userName": ADMIN_USER,
            "firstName": "OBSL",
            "lastName": "Demo",
            "email": "obsl@example.invalid",
            "createdAt": int(time.time() * 1000),
            "password": ADMIN_PASS,
        },
        timeout=10.0,
    )
    resp = client.post(
        "/apiv2/login",
        json={"userName": ADMIN_USER, "password": ADMIN_PASS},
        timeout=10.0,
    )
    resp.raise_for_status()
    return str(resp.json()["token"])


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"_dremio{token}"}


def _existing_source_state(client: httpx.Client, token: str, name: str, checks: dict) -> str | None:
    """Return 'match', 'drift', or None (absent) for an existing source.

    ``checks`` maps config keys to expected values; the special key
    ``propertyList`` maps property names to expected values. Secrets that
    Dremio does not echo back (passwords, secret keys) must not be in ``checks``.
    A stale Dremio (e.g. a source left over with the wrong host/endpoint) would
    otherwise pass a name-only check and fail later with missing-schema errors.
    """
    ent = client.get(f"/api/v3/catalog/by-path/{name}", headers=_headers(token), timeout=10.0)
    if ent.status_code != 200:
        return None
    cfg = ent.json().get("config", {}) or {}
    for key, expected in checks.items():
        if key == "propertyList":
            props = {p.get("name"): p.get("value") for p in cfg.get("propertyList", [])}
            if any(str(props.get(pk)) != str(pv) for pk, pv in expected.items()):
                return "drift"
        elif str(cfg.get(key)) != str(expected):
            return "drift"
    return "match"


def _delete_source(client: httpx.Client, token: str, name: str) -> None:
    ent = client.get(f"/api/v3/catalog/by-path/{name}", headers=_headers(token), timeout=10.0)
    if ent.status_code != 200:
        return
    sid = ent.json().get("id")
    if not sid:
        return
    client.delete(f"/api/v3/catalog/{quote(sid, safe='')}", headers=_headers(token), timeout=30.0)
    # Dremio deletes asynchronously; wait until the source is gone so the
    # subsequent recreate doesn't race into a 409 conflict.
    for _ in range(20):
        if (
            client.get(
                f"/api/v3/catalog/by-path/{name}", headers=_headers(token), timeout=10.0
            ).status_code
            != 200
        ):
            return
        time.sleep(1.0)


def _ensure_s3_source(client: httpx.Client, token: str) -> None:
    state = _existing_source_state(
        client,
        token,
        LAKE_SOURCE,
        {"propertyList": {"fs.s3a.endpoint": MINIO_ENDPOINT}},
    )
    if state == "match":
        print(f"  S3 source '{LAKE_SOURCE}' already exists (config matches)")
        return
    if state == "drift":
        print(f"  S3 source '{LAKE_SOURCE}' config drifted -> recreating")
        _delete_source(client, token, LAKE_SOURCE)
    body = {
        "entityType": "source",
        "name": LAKE_SOURCE,
        "type": "S3",
        "config": {
            "credentialType": "ACCESS_KEY",
            "accessKey": MINIO_KEY,
            "accessSecret": MINIO_SECRET,
            "secure": False,
            "rootPath": "/",
            "enableAsync": True,
            "compatibilityMode": True,
            "isCachingEnabled": True,
            "propertyList": [
                {"name": "fs.s3a.endpoint", "value": MINIO_ENDPOINT},
                {"name": "fs.s3a.path.style.access", "value": "true"},
                {"name": "dremio.s3.compat", "value": "true"},
            ],
        },
    }
    resp = client.post("/api/v3/catalog", json=body, headers=_headers(token), timeout=30.0)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"S3 source registration failed: {resp.status_code} {resp.text!r}")
    print(f"  S3 source '{LAKE_SOURCE}' -> {MINIO_ENDPOINT} registered")


def _promote_dataset(client: httpx.Client, token: str, table: str) -> str:
    path = [LAKE_SOURCE, BUCKET, table]
    by_path = "/".join(quote(p, safe="") for p in path)
    entity = client.get(f"/api/v3/catalog/by-path/{by_path}", headers=_headers(token), timeout=30.0)
    if entity.status_code != 200:
        return f"  ! {'.'.join(path)} not found ({entity.status_code})"
    data = entity.json()
    if data.get("entityType") == "dataset" or data.get("type") == "PHYSICAL_DATASET":
        return f"  = {'.'.join(path)} (already promoted)"
    ent_id = data["id"]
    body = {
        "entityType": "dataset",
        "id": ent_id,
        "path": path,
        "type": "PHYSICAL_DATASET",
        "format": {"type": "Parquet"},
    }
    resp = client.post(
        f"/api/v3/catalog/{quote(ent_id, safe='')}",
        json=body,
        headers=_headers(token),
        timeout=60.0,
    )
    if resp.status_code not in (200, 201):
        return f"  ! {'.'.join(path)} promote failed: {resp.status_code} {resp.text[:160]!r}"
    return f"  + {'.'.join(path)} promoted"


def _ensure_pg_source(client: httpx.Client, token: str) -> None:
    state = _existing_source_state(
        client,
        token,
        PG_SOURCE,
        {"hostname": PG_HOST, "port": PG_PORT, "databaseName": PG_DATABASE},
    )
    if state == "match":
        print(f"  pgwire source '{PG_SOURCE}' already exists (config matches)")
        return
    if state == "drift":
        print(f"  pgwire source '{PG_SOURCE}' config drifted -> recreating")
        _delete_source(client, token, PG_SOURCE)
    body = {
        "entityType": "source",
        "name": PG_SOURCE,
        "type": "POSTGRES",
        "config": {
            "hostname": PG_HOST,
            "port": PG_PORT,
            "databaseName": PG_DATABASE,
            "authenticationType": "MASTER",
            "username": "obsl",
            "password": "obsl",  # trust-auth: any password accepted
            "useSsl": False,
            "fetchSize": 200,
        },
        "metadataPolicy": {
            "authTTLMs": 86_400_000,
            "namesRefreshMs": 3_600_000,
            "datasetRefreshAfterMs": 3_600_000,
            "datasetExpireAfterMs": 10_800_000,
            "datasetUpdateMode": "PREFETCH_QUERIED",
        },
    }
    resp = client.post("/api/v3/catalog", json=body, headers=_headers(token), timeout=30.0)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"pgwire source registration failed: {resp.status_code} {resp.text!r}")
    print(
        f"  pgwire source '{PG_SOURCE}' -> {PG_HOST}:{PG_PORT} (database={PG_DATABASE}) registered"
    )


def _run_sql(
    client: httpx.Client, token: str, sql: str, timeout: float = 120.0
) -> list[list[object]]:
    submit = client.post("/api/v3/sql", json={"sql": sql}, headers=_headers(token), timeout=timeout)
    submit.raise_for_status()
    job_id = submit.json()["id"]
    deadline = time.monotonic() + timeout
    while True:
        status = client.get(f"/api/v3/job/{job_id}", headers=_headers(token), timeout=10.0)
        status.raise_for_status()
        state = status.json().get("jobState")
        if state == "COMPLETED":
            break
        if state in {"FAILED", "CANCELED"}:
            raise RuntimeError(
                f"job {job_id} {state}: {status.json().get('errorMessage', status.json())!r}"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} timed out")
        time.sleep(0.5)
    results = client.get(f"/api/v3/job/{job_id}/results", headers=_headers(token), timeout=30.0)
    results.raise_for_status()
    payload = results.json()
    cols = [c["name"] for c in payload.get("schema", [])]
    return [[row.get(c) for c in cols] for row in payload.get("rows", [])]


RAW_SQL = f"""
SELECT co.countryname AS country, CAST(SUM(s.salesamount) AS DECIMAL(18,2)) AS total_sales
FROM {LAKE_SOURCE}.{BUCKET}.sales s
JOIN {LAKE_SOURCE}.{BUCKET}.clients c ON s.salesclient = c.clientid
JOIN {LAKE_SOURCE}.{BUCKET}.countries co ON c.clientcountryid = co.countryid
GROUP BY co.countryname
ORDER BY total_sales DESC
LIMIT 5
""".strip()

GOVERNED_SQL = f"""
SELECT "Country Name", "Total Sales"
FROM {PG_SOURCE}.{PG_DATABASE}.model
ORDER BY "Total Sales" DESC
LIMIT 5
""".strip()


# Dremio Space that holds the demo views. Views (virtual datasets) cannot live
# inside a source (Dremio forbids ``CREATE VIEW <source>.…``); they belong in a
# Space or a user's home. Each view wraps one of the curated queries from
# demo-queries.sql so the demo shows "save a query as a reusable view": A1 is
# raw lakehouse SQL over the ``lake`` source; A2-A7 are governed queries over
# OrionBelt. When a governed view is queried, Dremio wraps its body in a
# derived table and pushes it to OrionBelt, which flattens it back to flat OBSQL.
VIEW_SPACE = "governed"
_M = f"{PG_SOURCE}.{PG_DATABASE}.model"
_LAKE = f"{LAKE_SOURCE}.{BUCKET}"
DEMO_VIEWS: tuple[tuple[str, str], ...] = (
    (
        "raw_top_countries",  # A1 - raw lakehouse SQL (no OrionBelt)
        f"SELECT co.countryname AS country, "
        f"CAST(SUM(s.salesamount) AS DECIMAL(18,2)) AS total_sales "
        f"FROM {_LAKE}.sales s "
        f"JOIN {_LAKE}.clients c ON s.salesclient = c.clientid "
        f"JOIN {_LAKE}.countries co ON c.clientcountryid = co.countryid "
        f"GROUP BY co.countryname ORDER BY total_sales DESC LIMIT 5",
    ),
    (
        "top_countries_by_sales",  # A2 - same answer, governed
        f'SELECT "Country Name", "Total Sales" FROM {_M} ORDER BY "Total Sales" DESC LIMIT 5',
    ),
    (
        "clients_in_singapore",  # A3a - dimension filter -> WHERE
        f'SELECT "Client Name", "Total Sales" FROM {_M} '
        'WHERE "Country Name" = \'Singapore\' ORDER BY "Total Sales" DESC LIMIT 5',
    ),
    (
        "countries_over_1m",  # A3b - measure filter -> HAVING
        f'SELECT "Country Name", "Total Sales" FROM {_M} '
        'WHERE "Total Sales" > 1000000 ORDER BY "Total Sales" DESC LIMIT 5',
    ),
    (
        "sales_vs_shipments",  # A4 - cross-fact, Composite Fact Layer
        f'SELECT "Year Month", "Total Sales", "Total Shipments" FROM {_M} '
        'ORDER BY "Year Month" LIMIT 12',
    ),
    (
        "avg_sale_by_channel",  # A5 - governed metric
        f'SELECT "Channel Name", "Total Sales", "Average Sale" FROM {_M} '
        'ORDER BY "Total Sales" DESC',
    ),
    (
        "sales_period_over_period",  # A6 - MoM + YoY window metrics
        f'SELECT "Sales Month", "Total Sales", "Sales MoM Change", "Sales YoY Growth" '
        f'FROM {_M} ORDER BY "Sales Month" LIMIT 15',
    ),
    (
        "category_margin",  # A7 - cross-fact derived metrics
        f'SELECT "Product Category", "Total Sales", "Return Rate", "Gross Margin" '
        f'FROM {_M} ORDER BY "Total Sales" DESC LIMIT 5',
    ),
)


def _ensure_space(client: httpx.Client, token: str, name: str) -> None:
    """Create a Dremio Space (idempotent) to hold the demo views."""

    existing = client.get(f"/api/v3/catalog/by-path/{name}", headers=_headers(token), timeout=10.0)
    if existing.status_code == 200:
        print(f"  Space '{name}' already exists")
        return
    resp = client.post(
        "/api/v3/catalog",
        json={"entityType": "space", "name": name},
        headers=_headers(token),
        timeout=30.0,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Space '{name}' creation failed: {resp.status_code} {resp.text!r}")
    print(f"  Space '{name}' created")


def _ensure_views(client: httpx.Client, token: str) -> None:
    """Create the demo views in the Space (idempotent via CREATE OR REPLACE)."""

    for name, body in DEMO_VIEWS:
        ddl = f'CREATE OR REPLACE VIEW "{VIEW_SPACE}"."{name}" AS {body}'
        try:
            _run_sql(client, token, ddl)
            print(f"  view '{VIEW_SPACE}.{name}' created")
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest
            print(f"  view '{VIEW_SPACE}.{name}' FAILED: {exc}")


def _print_compare(raw: list[list[object]], gov: list[list[object]]) -> None:
    print("\n" + "=" * 64)
    print("  Top 5 countries by Total Sales - RAW Parquet vs GOVERNED (OBSL)")
    print("=" * 64)
    print(f"  {'Country':<22}{'RAW (Dremio SQL)':>20}{'GOVERNED (OBSL)':>20}")
    print("  " + "-" * 60)
    gov_map = {str(r[0]): r[1] for r in gov}
    for country, raw_val in raw:
        gov_val = gov_map.get(str(country), "-")
        print(f"  {str(country):<22}{str(raw_val):>20}{str(gov_val):>20}")


def main() -> int:
    with httpx.Client(base_url=DREMIO_REST) as client:
        print(f"Logging into Dremio at {DREMIO_REST} ...")
        token = _login(client)

        print("Registering S3 (MinIO) source ...")
        _ensure_s3_source(client, token)

        print("Promoting Parquet datasets ...")
        for table in TABLES:
            print(_promote_dataset(client, token, table))

        print("Registering pgwire source -> OrionBelt ...")
        _ensure_pg_source(client, token)

        print(f"Creating demo views in Space '{VIEW_SPACE}' ...")
        _ensure_space(client, token, VIEW_SPACE)
        _ensure_views(client, token)

        print("\nRunning RAW Parquet query in Dremio ...")
        raw = _run_sql(client, token, RAW_SQL)
        print("Running GOVERNED query (federated through OrionBelt) ...")
        gov = _run_sql(client, token, GOVERNED_SQL)
        _print_compare(raw, gov)

    print("\nDone. Open the Dremio UI: http://localhost:19047")
    print(f"  login: {ADMIN_USER} / {ADMIN_PASS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

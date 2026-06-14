# Authentication

OrionBelt ships with authentication **off by default** — the public demo and
local-dev (`uv run orionbelt-api`) keep working with no credentials. Production
deployments turn it on with a single environment variable that governs **every
surface**: REST, Arrow Flight SQL, the Postgres wire protocol, the Gradio UI,
and the MCP server.

## TL;DR

```bash
# 1. Generate a key
KEY="obsl_pat_$(python3 -c 'import secrets; print(secrets.token_hex(20))')"
echo "$KEY"

# 2. Turn on auth + register the key
export AUTH_MODE=api_key
export API_KEYS="$KEY"

# 3. Restart OrionBelt — every surface now requires this key
```

One variable, three protocols, two consumers. Rotation is the same variable
with a comma-separated second key.

## The `AUTH_MODE` selector

| Mode | Meaning |
|------|---------|
| `none` | No authentication (default). Preserves public-demo / local-dev behaviour. |
| `api_key` | Validate a key from `API_KEYS` on every request. |
| `oidc` | OpenID Connect / JWT (planned; rejected at startup until it ships). |

### Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_MODE` | `none` | `none`, `api_key`, or `oidc`. |
| `API_KEYS` | — | Comma-separated valid keys (≥32 chars, high-entropy). Required when `AUTH_MODE=api_key`. |
| `API_KEY_HEADER` | `X-API-Key` | REST header name. `Authorization: Bearer` is always accepted as a fallback. |
| `AUTH_ENABLED` | `false` | Deprecated alias for `AUTH_MODE=api_key` (honoured one release with a startup warning). |

Startup fails fast (refuses to boot) if `AUTH_MODE=api_key` with an empty
`API_KEYS`, or if any key is shorter than 16 characters.

## Generating keys

Out-of-band — there is no in-app key generation endpoint. Use any secure RNG:

```bash
# Python (matches OrionBelt's recommended format)
python3 -c "import secrets; print(f'obsl_pat_{secrets.token_hex(20)}')"

# OpenSSL
echo "obsl_pat_$(openssl rand -hex 20)"
```

The `obsl_pat_` prefix is a convention, not a constraint. Keys must be **at
least 32 characters and high-entropy** — the server refuses to start on a short
or low-entropy key (these are vulnerable to offline attack on captured SCRAM
transcripts). The recommended 40-hex-char token easily satisfies this. The
prefix makes a leaked key easy to spot in code scanners and log alerting.

## Detecting whether auth is required

`GET /health` is always unauthenticated and reports the active mode, so a
client can check before sending credentials:

```bash
curl -s http://localhost:8000/health
# {"status":"ok","version":"...","auth_mode":"api_key"}
```

## Client recipes

### REST (curl, httpx, requests)

```bash
# X-API-Key header (recommended)
curl -H "X-API-Key: $KEY" http://localhost:8000/v1/schema

# Authorization: Bearer (fallback for tools that can't set custom headers)
curl -H "Authorization: Bearer $KEY" http://localhost:8000/v1/schema
```

`/health`, `/robots.txt`, `/docs`, `/redoc`, `/openapi.json`, and `/ui` stay
unauthenticated; everything under `/v1` requires a key.

### Arrow Flight SQL (DBeaver, Tableau, programmatic)

DBeaver / Flight JDBC: set the **username** to anything (e.g. `token`, it is
ignored) and the **password** to your API key.

```python
import pyarrow.flight as flight

client = flight.FlightClient("grpc://localhost:8815")
token = client.authenticate_basic_token(b"token", KEY.encode())
options = flight.FlightCallOptions(headers=[token])
# pass options on every call
```

The legacy `FLIGHT_AUTH_MODE=token` / `FLIGHT_API_TOKEN` still works for one
release with a deprecation warning. Migrate to `AUTH_MODE=api_key` + `API_KEYS`.

!!! warning "Flight can run unauthenticated"
    The Flight server is opt-in: it starts only when `FLIGHT_ENABLED=true` (it
    does not auto-start just because `ob-flight-extension` is installed). Once
    enabled it binds `0.0.0.0`, and unless `AUTH_MODE=api_key` (or the legacy
    `FLIGHT_AUTH_MODE=token` **with** `FLIGHT_API_TOKEN`) is set it accepts every
    client (the server logs a loud warning). Setting `FLIGHT_API_TOKEN` alone,
    without `FLIGHT_AUTH_MODE=token`, does **not** enable auth. For any non-local
    deployment, set `AUTH_MODE=api_key` (Flight then validates against the shared
    key store) or restrict access to the Flight port at the network layer.

### Postgres wire (psql, Tableau, Power BI, Metabase, DBeaver)

When `AUTH_MODE=api_key`, pgwire requires a password (the API key). The
username is ignored — pick anything readable in your logs.

```bash
psql "postgresql://obsl:$KEY@localhost:5432/__default__"
```

By default pgwire uses **SCRAM-SHA-256**, which never sends the key on the
wire. Clients that lack SCRAM support can fall back to cleartext password auth
by setting `PGWIRE_AUTH_MODE=password` on the server — in that case terminate
TLS in front of pgwire on untrusted networks, since the key is sent in plain.

### Gradio UI

The UI is a thin REST client. Start it with the key in its environment;
browser users never see it:

```bash
export OBSL_API_KEY="$KEY"
uv run orionbelt-ui
```

If `OBSL_API_KEY` is unset while the API enforces auth, the UI logs a clear
startup error (rather than surfacing cryptic 401s in the browser).

!!! warning "The UI is a privileged proxy"
    The UI holds an API key and can act on `/v1` (create sessions, load models,
    run queries, clear cache). `/ui` itself is **not** behind API-key auth
    (browsers cannot send the key on navigation), so anyone who can reach `/ui`
    acts as the key holder. For this reason the **embedded** (co-hosted) UI does
    **not** auto-adopt a key from `API_KEYS` - you must set `OBSL_API_KEY`
    explicitly, and when you do, restrict network access to `/ui` (reverse proxy
    / firewall / private network).

### MCP server

The MCP server is a thin HTTP client of the REST API (separate repository). Set
the key once in its environment; it forwards the key on every upstream call.
LLM agents talking to MCP over stdio never see it.

```bash
export OBSL_API_KEY="$KEY"
```

## Rotation

Multiple keys are valid simultaneously, so rotation is zero-downtime:

```bash
export API_KEYS="$OLD_KEY,$NEW_KEY"   # both work; migrate clients to the new key
export API_KEYS="$NEW_KEY"            # remove the old key; redeploy
```

Editing the variable and restarting is the revocation mechanism — there is no
separate revocation endpoint at this scale.

## Where keys live

`API_KEYS` is read from the environment; choose the mechanism that fits your
deployment:

| Deployment | How |
|------------|-----|
| Local dev | `.env` file (`API_KEYS=obsl_pat_...`) |
| Docker | `-e API_KEYS=obsl_pat_...` |
| Cloud Run | Secret Manager → `--set-secrets=API_KEYS=obsl-api-keys:latest` |
| Kubernetes | `secretKeyRef` in the Pod spec |

## Production checklist

- [ ] `AUTH_MODE=api_key`
- [ ] `API_KEYS` set to at least one random key (≥40 chars recommended)
- [ ] Keys stored in your platform's secret manager, not committed to a repo
- [ ] HTTPS / TLS terminating in front of OrionBelt (reverse proxy or platform)
- [ ] Rotation cadence documented (90 days is a reasonable default)
- [ ] Public demo deployments use a separate key set from production

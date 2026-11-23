---
description: "Authentication is off by default and turns on with one environment variable that governs every surface: REST, Arrow Flight SQL, the Postgres wire protocol, the Gradio UI and MCP. API keys, or the access tokens of an OpenID Connect provider."
---

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
| `oidc` | REST verifies the access tokens of your OpenID Connect provider (Keycloak, Microsoft Entra ID, Auth0, Okta, ...). Keys in `API_KEYS` stay valid as service keys. See [OpenID Connect](#openid-connect). |

### Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_MODE` | `none` | `none`, `api_key`, or `oidc`. |
| `API_KEYS` | — | Comma-separated valid keys (≥32 chars, high-entropy). Required when `AUTH_MODE=api_key`. |
| `API_KEY_HEADER` | `X-API-Key` | REST header name. `Authorization: Bearer` is always accepted as a fallback. |
| `AUTH_ENABLED` | `false` | Deprecated alias for `AUTH_MODE=api_key` (honoured one release with a startup warning). |

Startup fails fast (refuses to boot) if `AUTH_MODE=api_key` with an empty
`API_KEYS`, or if any key is shorter than 32 characters or low-entropy.

## Transport security (TLS)

Authentication proves *who* is calling; TLS stops anyone else reading what they
said. They are separate settings and both matter: SCRAM keeps a pgwire key off
the wire even without TLS, but the query results still cross it in the clear.

All three surfaces take the same three settings, read by the same loader, and
each names its own prefix in every error message:

| Surface | Certificate | Key | Client CA (mutual TLS) |
|---|---|---|---|
| REST | `API_TLS_CERT` | `API_TLS_KEY` | `API_TLS_CLIENT_CA` |
| Postgres wire | `PGWIRE_TLS_CERT` | `PGWIRE_TLS_KEY` | `PGWIRE_TLS_CLIENT_CA` |
| Arrow Flight SQL | `FLIGHT_TLS_CERT` | `FLIGHT_TLS_KEY` | `FLIGHT_TLS_CLIENT_CA` |

```bash
API_TLS_CERT=/certs/server.crt \
API_TLS_KEY=/certs/server.key \
API_TLS_CLIENT_CA=/certs/ca.crt \
uv run orionbelt-api
```

The startup line says which you got, so confirming TLS is live needs no packet
capture:

```
REST API serving HTTPS on 0.0.0.0:8000 (client certificates required)
```

**Both settings or neither.** A certificate without its key refuses to start
rather than falling back to plaintext, because a deployment that reads as
encrypted and is not is worse than one that does not come up. A client CA
without a server certificate is refused for the same reason: mutual TLS needs
both halves.

**When you need this and when you do not.** Behind Cloud Run, an ingress or
nginx, TLS is already terminated at the edge and these can stay unset - that is
why they default to off and why every existing deployment is unchanged. Set them
when there is no proxy (a LAN or on-premise deployment), when the hop between
the proxy and OrionBelt must be encrypted too, or when you want mutual TLS all
the way to the application rather than to the edge.

The [CLI](cli.md#tls-in-remote-mode) can present a client certificate with
`--client-cert` / `--client-key`, so `obsl --server` works against a listener
configured with `API_TLS_CLIENT_CA`.

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

## OpenID Connect

With `AUTH_MODE=oidc`, users and services sign in with your identity provider
(IdP) and send its **access token** to the REST API:

```bash
curl -H "Authorization: Bearer $ACCESS_TOKEN" https://obsl.example.com/v1/schema
```

```bash
export AUTH_MODE=oidc
export OIDC_ISSUER=https://keycloak.example.com/realms/analytics
export OIDC_AUDIENCE=obsl-api
export API_KEYS="$SERVICE_KEY"   # optional: service keys next to the tokens
```

| Variable | Default | Description |
|----------|---------|-------------|
| `OIDC_ISSUER` | — | Required. The tokens' `iss`, exactly (a trailing slash counts). |
| `OIDC_AUDIENCE` | — | Required. A token's `aud` must include it: the audience your IdP issues for the OrionBelt API. |
| `OIDC_JWKS_URI` | from discovery | Where the IdP publishes its signing keys. Unset, it is read from `{OIDC_ISSUER}/.well-known/openid-configuration`. |
| `OIDC_JWKS_FILE` | — | A JSON file with the signing keys, for air-gapped installs. Nothing is fetched. |
| `OIDC_JWKS_CACHE_TTL` | `86400` | Seconds before the keys are fetched again. |
| `OIDC_JWKS_MIN_REFETCH_SECONDS` | `60` | A token signed with a key OrionBelt does not have fetches the keys again, at most this often. |
| `OIDC_SCOPE_CLAIMS` | `scope,scp,roles` | Claims that carry scopes or roles, merged. A dot reaches into an object: `realm_access.roles`. |
| `OIDC_SCOPE_MAP` | — | IdP names that grant OrionBelt scopes, e.g. `analysts=obsl:read,admins=obsl:admin`. Repeat a name to grant several. |
| `OIDC_CLOCK_SKEW_SECONDS` | `60` | Leeway on the token's expiry and not-before times. |

### What a token must pass

- A signature by one of the issuer's keys, with RS256, PS256, ES256 or EdDSA. `none` and
  shared-secret (HS256) tokens are refused.
- `iss` equal to `OIDC_ISSUER`, `aud` including `OIDC_AUDIENCE`.
- Not expired, not before its `nbf`, within `OIDC_CLOCK_SKEW_SECONDS`.
- A `sub`: the user (or service account) the request runs as.

Send the access token, not the ID token. If the ID token's audience (the UI's
client ID) were `OIDC_AUDIENCE`, ID tokens would pass too, so give the API an
audience of its own.

OrionBelt loads the signing keys at startup and **refuses to start** when it
cannot. When the IdP rotates its keys, the first token signed with the new key
makes OrionBelt fetch them again. A fetch that fails keeps the keys it has.

### Scopes

Every `/v1` endpoint requires one scope. A higher scope includes the lower ones.

| Scope | Allows |
|-------|--------|
| `obsl:read` | Read models, compile and run queries, start a session of your own |
| `obsl:write` | Also: load a model into a session or remove one, close a session, `POST /v1/oneshot/batch` (it runs the model it is sent) |
| `obsl:admin` | Also: the result and compilation caches (`/v1/cache/...`), and every user's sessions |

A token gets a scope when one of the `OIDC_SCOPE_CLAIMS` holds it, either as
`obsl:read` itself or as a name `OIDC_SCOPE_MAP` maps to it. A token with none
of them is refused on every endpoint (403). API keys hold all three.

### Sessions belong to whoever created them

A session a user creates is visible to that user and to admins only. Anyone
else gets 404 for it, the same as for a session that does not exist, and
`GET /v1/sessions` and the top-level shortcuts (`/v1/query/execute`, ...) only
see the caller's own sessions. Sessions holding `MODEL_FILES` models are
shared, and so are sessions created with an API key.

### Errors

| Situation | Status | `WWW-Authenticate` |
|-----------|--------|--------------------|
| No credential | 401 | `Bearer realm="obsl"` |
| Token does not verify (expired, wrong issuer, ...) | 401 | `Bearer realm="obsl", error="invalid_token", error_description="Token has expired"` |
| Missing scope | 403 | `Bearer realm="obsl", error="insufficient_scope", scope="obsl:write"` |
| Wrong API key | 403 | — |

The response body names the reason too: `{"detail": {"code": "AUTH_INVALID", "message": "Token has expired"}}`.

### The other surfaces under `oidc`

- **Arrow Flight SQL and the Postgres wire protocol accept API keys only**, not
  the IdP's tokens: use a key from `API_KEYS`, as in `api_key` mode. With
  `FLIGHT_ENABLED` or `PGWIRE_ENABLED` and no `API_KEYS`, OrionBelt refuses to
  start rather than open a listener nobody can use.
- **The Gradio UI and the MCP server** use a service key as `OBSL_API_KEY`.
  Signing in to the UI with the IdP is not available yet.
- An API key may not contain a `.` under `oidc` (a key shaped like a JWT would
  be read as one).

### Identity provider notes

| IdP | `OIDC_ISSUER` | `OIDC_AUDIENCE` | Scopes |
|-----|---------------|-----------------|--------|
| Keycloak | `https://<host>/realms/<realm>` | Add an *Audience* mapper to the client, e.g. `obsl-api`: Keycloak puts no API audience in access tokens by default | Realm roles are in `realm_access.roles`: `OIDC_SCOPE_CLAIMS=scope,realm_access.roles`, and map role names with `OIDC_SCOPE_MAP` |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant id>/v2.0`. Set `accessTokenAcceptedVersion` to `2` in the API app's manifest: otherwise Entra issues v1 tokens, whose issuer is `https://sts.windows.net/<tenant id>/` | The API app registration's Application (client) ID | Delegated scopes are in `scp`, application permissions (services, client credentials) in `roles`: both are in the default `OIDC_SCOPE_CLAIMS` |
| Auth0 | `https://<tenant>.auth0.com/` (with the trailing slash) | The API's Identifier | With RBAC and *Add Permissions in the Access Token* on, permissions are in `permissions`: `OIDC_SCOPE_CLAIMS=scope,permissions` |

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
    enabled it binds `0.0.0.0`, and unless `AUTH_MODE` is `api_key` or `oidc` (or the legacy
    `FLIGHT_AUTH_MODE=token` **with** `FLIGHT_API_TOKEN`) is set it accepts every
    client (the server logs a loud warning). Setting `FLIGHT_API_TOKEN` alone,
    without `FLIGHT_AUTH_MODE=token`, does **not** enable auth. For any non-local
    deployment, set `AUTH_MODE=api_key` (Flight then validates against the shared
    key store) or restrict access to the Flight port at the network layer.

### Postgres wire (psql, Tableau, Power BI, Metabase, DBeaver)

When `AUTH_MODE` is `api_key` or `oidc`, pgwire requires a password (an API
key). The username is ignored — pick anything readable in your logs.

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

- [ ] `AUTH_MODE=api_key`, or `AUTH_MODE=oidc` with `OIDC_ISSUER` and `OIDC_AUDIENCE`
- [ ] `API_KEYS` set to at least one random key (≥40 chars recommended); under `oidc`, only if services, Flight SQL or pgwire need one
- [ ] Keys stored in your platform's secret manager, not committed to a repo
- [ ] TLS on every exposed surface: either terminated in front (reverse proxy or platform) or served by OrionBelt itself with `API_TLS_*` / `PGWIRE_TLS_*` / `FLIGHT_TLS_*` - see [Transport security](#transport-security-tls)
- [ ] Rotation cadence documented (90 days is a reasonable default)
- [ ] Public demo deployments use a separate key set from production

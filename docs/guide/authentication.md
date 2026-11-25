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

Startup fails fast (refuses to boot) if `AUTH_MODE=api_key` with an empty
`API_KEYS`, or if any key is shorter than 32 characters or low-entropy.

!!! note "Removed settings"
    `AUTH_ENABLED`, `FLIGHT_AUTH_MODE` and `FLIGHT_API_TOKEN` were removed. If
    one is still set to turn authentication on (`AUTH_ENABLED=true`,
    `FLIGHT_AUTH_MODE=token`, any `FLIGHT_API_TOKEN`), OrionBelt refuses to
    start and names it, rather than starting without the authentication it
    asked for. Use `AUTH_MODE=api_key`, and put a Flight token into `API_KEYS`.

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
| `OIDC_SWAGGER_CLIENT_ID` | — | A public client at the IdP for signing in on `/docs`. See [Swagger UI](#swagger-ui-docs). |
| `OIDC_SWAGGER_SCOPES` | `openid` | Scopes `/docs` asks for at sign-in, space-separated. |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` or `OIDC_CLIENT_SECRET_FILE` | — | OrionBelt's own confidential client at the IdP. It asks for a Snowflake token on a signed-in user's behalf: see [Snowflake through Entra ID](warehouse-sso/snowflake.md). It also gives a BI token its warehouse sign-in: see [Data sources that log in as you](bi-tokens.md#data-sources-that-log-in-as-you). |
| `OIDC_AUTHORIZATION_ENDPOINT`, `OIDC_TOKEN_ENDPOINT` | from discovery | Where users sign in and codes are exchanged. Only needed with `OIDC_JWKS_URI` or `OIDC_JWKS_FILE`, which skip the discovery document. |
| `OIDC_REVOCATION_ENDPOINT` | from discovery | Where OrionBelt revokes a revoked BI token's warehouse sign-in. Entra ID has none. |

A data source can also **log in to the database as the signed-in user**, so the
database applies that user's own grants and row-level security: see
[Warehouse Single Sign-On](warehouse-sso/index.md). BI tools, which cannot sign in, use
a long-lived token you create for them: see [Tokens for BI Tools](bi-tokens.md).

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

Arrow Flight SQL and the Postgres wire protocol take the same access token as
the password (or a service key from `API_KEYS`), need `obsl:read`, and run as
the token's user: one user's session is not reachable by another there either.
A Flight ticket or prepared statement works only for the caller it was issued
to; anyone else presenting it gets "unknown", as for one that does not exist.

| Surface | Token | API key | When the token expires |
|---------|-------|---------|------------------------|
| Arrow Flight SQL | As the password of a Basic credential, or `Authorization: Bearer` | Yes | Every call checks it, so the next call fails. BI tools cannot refresh a token: give a long-running BI connection a service key |
| Postgres wire | As the password, **over TLS only** (`sslmode=require`; needs `PGWIRE_TLS_CERT`). On TLS, pgwire asks for a cleartext password, which TLS protects; SCRAM cannot check a token. A [BI token](bi-tokens.md) also works without TLS, by SCRAM, with its id as the user name | Yes, with or without TLS | The connection was authenticated when it opened and stays open, as a Postgres connection does. A [BI token](bi-tokens.md) is checked again before each query: revoked or expired, it ends the connection |

With `PGWIRE_ENABLED`, OrionBelt refuses to start under `oidc` without
`API_KEYS`, `PGWIRE_TLS_CERT` or a token store for [BI tokens](bi-tokens.md)
(`TOKEN_STORE_PATH`), since no client could log in.

The **Gradio UI** and the **MCP server** use a service key as `OBSL_API_KEY`.
Signing in to the UI with the IdP is not available yet.

An API key may not contain a `.` under `oidc` (a key shaped like a JWT would be
read as one).

### Swagger UI (`/docs`)

With authentication on, the **Authorize** button on `/docs` offers what the API
accepts: an API key in `api_key` mode; under `oidc` a pasted access token, an
API key when `API_KEYS` is set, and, with `OIDC_SWAGGER_CLIENT_ID`, signing in
with the IdP. Swagger then sends the token on every call it makes, and scopes
apply as for any other caller.

For the sign-in, register a **public client** at the IdP:

- Authorization code flow with PKCE (`S256`). No client secret: it would be in
  the browser.
- Redirect URI `<API URL>/docs/oauth2-redirect`, for example
  `https://obsl.example.com/docs/oauth2-redirect`.
- Allowed web origin `<API URL>`: the browser exchanges the code for a token at
  the IdP's token endpoint itself (on Keycloak, *Web origins*).
- The same audience as the API's tokens (`OIDC_AUDIENCE`). OrionBelt sends it
  as the `audience` parameter, which Auth0 needs and other IdPs ignore.

```bash
export OIDC_SWAGGER_CLIENT_ID=obsl-swagger
export OIDC_SWAGGER_SCOPES="openid"
```

What to ask for depends on the IdP, because the token must be issued **for
OrionBelt** (its `aud` is `OIDC_AUDIENCE`):

| IdP | `OIDC_SWAGGER_SCOPES` |
|-----|-----------------------|
| Microsoft Entra ID | `openid api://<OrionBelt app ID>/<scope>`: a scope the OrionBelt app registration exposes. With `openid` alone Entra issues a token for Microsoft Graph, which OrionBelt refuses for its audience. |
| Okta | `openid` plus the scopes of the custom authorization server whose audience is `OIDC_AUDIENCE` |
| Keycloak | `openid`; the audience comes from the client's *Audience* mapper |
| Auth0 | `openid`; OrionBelt sends `OIDC_AUDIENCE` as the `audience` parameter |

A token from this sign-in is also the quickest way to try OrionBelt as a
signed-in user, for example to run a query as yourself.

The authorization and token endpoints are read from the discovery document.
The page's Content-Security-Policy lets `/docs` reach the issuer's origin (and
that of `OIDC_TOKEN_ENDPOINT`, when set). Without `OIDC_SWAGGER_CLIENT_ID`,
`/docs` takes a pasted token or key.

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

Under `AUTH_MODE=oidc` the password may be an access token instead; see
[the other surfaces under `oidc`](#the-other-surfaces-under-oidc).

!!! warning "Flight can run unauthenticated"
    The Flight server is opt-in: it starts only when `FLIGHT_ENABLED=true` (it
    does not auto-start just because `ob-flight-extension` is installed). Once
    enabled it binds `0.0.0.0`, and unless `AUTH_MODE` is `api_key` or `oidc` it
    accepts every client (the server logs a loud warning). For any non-local
    deployment, set `AUTH_MODE` (Flight then authenticates every call the way
    REST does) or restrict access to the Flight port at the network layer.

### Postgres wire (psql, Tableau, Power BI, Metabase, DBeaver)

When `AUTH_MODE` is `api_key` or `oidc`, pgwire requires a password (an API
key). The username is ignored, so pick anything readable in your logs, with
one exception: under `oidc` with [BI tokens](bi-tokens.md) on, a SCRAM login
(the default without TLS) whose username is 12 lowercase hex characters, or
`tok_` and them, is a BI token's login and takes no API key. `obsl` is
always safe.

```bash
psql "postgresql://obsl:$KEY@localhost:5432/__default__"
```

By default pgwire uses **SCRAM-SHA-256**, which never sends the key on the
wire. Clients that lack SCRAM support can fall back to cleartext password auth
by setting `PGWIRE_AUTH_MODE=password` on the server — in that case use TLS
(`PGWIRE_TLS_CERT`) on untrusted networks, since the key is sent in plain.

Under `AUTH_MODE=oidc` the password may be an access token, over TLS:

```bash
psql "postgresql://obsl:$ACCESS_TOKEN@localhost:5432/__default__?sslmode=require"
```

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

Before you remove the old key, check that nothing still uses it: its
`last_used_since_start` in [`GET /v1/auth/status`](#checking-the-setup).

## Checking the setup

### Key ids

OrionBelt never shows an API key. It names each one by its **id**: eight hex
characters of a SHA-256 of the key, in startup lines, error messages and the
status endpoint. The id says nothing about the key. To find which key an id
is, compute it:

```bash
python3 -c "import hashlib, sys; print(hashlib.sha256(b'obsl-key:' + sys.argv[1].encode()).hexdigest()[:8])" "$KEY"
```

### At startup

OrionBelt logs one line per part, for example:

```text
Auth: oidc; issuer https://login.example.com/realms/obsl; audience obsl; 2 service key(s) (3f2a9c41, 77b0e1d2)
Auth: oidc keys loaded from https://login.example.com/realms/obsl/protocol/openid-connect/certs (2 key(s))
Auth: BI tokens on (store ./tokens/bi_tokens.sqlite: 14 active, 9 with a warehouse sign-in; warehouse sign-ins can be made)
Auth: data sources that log in as the user: sf (snowflake, token_exchange), dbx (databricks, token federation)
pgwire surface enabled on 0.0.0.0:5432 (AUTH_MODE=oidc, SCRAM-SHA-256, TLS off, semantic=True)
Flight SQL server started on port 8815 (vendor=duckdb, AUTH_MODE=oidc)
```

### `GET /v1/auth/status`

An admin (`obsl:admin`) gets the same, and more, as JSON. It never contains
a secret:

| Part | What it says |
|---|---|
| `mode`, `api_key_header` | `AUTH_MODE`, and the header API keys are read from |
| `service_keys` | Each API key's id, and when it last authenticated a caller since OrionBelt started (`null`: not since then). Kept in memory only: a restart starts over |
| `oidc` | Issuer, audience, the sign-in, token and revocation endpoints in use (set or discovered), the signing keys (where from, how many, when loaded), the scope claims and map, OrionBelt's own client ID |
| `bi_tokens` | On or off, the store, how many tokens are active and how many hold a warehouse sign-in, the default and longest lifetime |
| `warehouse_sign_in` | Whether a new BI token gets a warehouse sign-in, or what it still needs |
| `user_login_sources` | The data sources that log in as the user, and how each gets the user's warehouse token |
| `surfaces` | Flight SQL and the Postgres wire protocol (on, TLS, how a client logs in), the `/tokens` page, and the sign-in on `/docs` |

```bash
curl -s https://obsl.example.com/v1/auth/status -H "Authorization: Bearer $ADMIN_TOKEN"
```

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

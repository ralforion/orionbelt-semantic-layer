# Tokens for BI Tools

BI tools (Tableau, Power BI, DBeaver, Excel, any ODBC or JDBC driver) store a
password and send it every time. They cannot sign in with your identity
provider, and its access tokens expire within the hour. A **BI token** is a
long-lived password you create for one tool: it acts as you, with the scopes
you gave it, until it expires or you revoke it.

BI tokens exist under `AUTH_MODE=oidc` only: a token belongs to a person, so
someone has to be signed in to create one.

## What a token is

| | |
|---|---|
| Looks like | `obsl_tok_3f2a9c41b7e0_9d…` (an id, then a secret of 160 random bits) |
| Acts as | You: your sessions, your name in OrionBelt's logs |
| Can do | The scopes you gave it, a subset of yours; `obsl:read` by default |
| Lasts | 90 days by default (`TOKEN_DEFAULT_DAYS`), at most `TOKEN_MAX_DAYS` (365); no token lasts forever |
| Is shown | Once, when you create it. OrionBelt keeps only a hash of it. |
| Stops | When it expires, or at once when it is revoked, also on a Postgres connection that is already open (it ends at its next query) |

## Create one

### On the token page

Open `https://obsl.example.com/tokens`, sign in with your identity provider,
and fill in a label, the scopes and the number of days. The page shows the
new token once, with a **Copy** button, and lists your tokens with a
**Revoke** button each. Your sign-in stays in the page only: reloading it
asks you to sign in again (usually without typing anything, while your
identity provider still knows you).

The page signs in as its own client at your identity provider, separate
from the one Swagger's `/docs` uses, so each can be allowed or turned off on
its own. Set it up once:

| Identity provider | The page's client |
|---|---|
| Keycloak | A public client (**Client authentication** off) with **Standard flow** on and PKCE `S256`; **Valid redirect URIs** `https://obsl.example.com/tokens`; **Web origins** `https://obsl.example.com` (the page fetches the token from the browser). Its tokens must name OrionBelt's audience, as for any sign-in. |
| Entra ID | An app registration with the redirect URI `https://obsl.example.com/tokens` under the **Single-page application** platform; `OIDC_TOKENS_SCOPES` asks for OrionBelt's scope, `openid api://<OrionBelt app ID>/<scope>`. |
| Okta | A **Single-Page Application** integration with the sign-in redirect URI `https://obsl.example.com/tokens`, and `https://obsl.example.com` as a trusted origin. |

```bash
OIDC_TOKENS_CLIENT_ID=<the page's client ID>
OIDC_TOKENS_SCOPES=openid            # Entra: openid api://<OrionBelt app ID>/<scope>
```

Without `OIDC_TOKENS_CLIENT_ID` there is no page (`/tokens` answers 404);
the API below still works.

### With the API

Call OrionBelt with your own sign-in token (the access token your identity
provider gave you):

```bash
curl -s -X POST https://obsl.example.com/v1/auth/tokens \
  -H "Authorization: Bearer $MY_ACCESS_TOKEN" -H "Content-Type: application/json" \
  -d '{"label": "Tableau, laptop", "scopes": ["obsl:read"], "days": 90}'
```

```json
{
  "id": "3f2a9c41b7e0",
  "label": "Tableau, laptop",
  "owner": "jane@example.com",
  "scopes": ["obsl:read"],
  "created_at": "2026-11-24T10:00:00Z",
  "expires_at": "2027-02-22T10:00:00Z",
  "last_used_at": null,
  "token": "obsl_tok_3f2a9c41b7e0_9d…"
}
```

Copy `token` into the tool now: it is not shown again. A token gets only
scopes you hold (`obsl:write` includes `obsl:read`). An API key, or another
BI token, cannot create one.

## Use it in a tool

| Surface | Where the token goes |
|---|---|
| REST | `Authorization: Bearer obsl_tok_…`, or the API key header |
| Arrow Flight SQL (DBeaver, Tableau, JDBC, ODBC) | Username `token`, password = the token |
| Postgres protocol (Tableau, Power BI, psql) | Password = the token, any user name, over **TLS** (`sslmode=require`). Without TLS it is refused. The token is checked again before each query, so an open connection ends once the token is revoked or expires. |

## List and revoke

```bash
# Your tokens (never their secret)
curl -s https://obsl.example.com/v1/auth/tokens -H "Authorization: Bearer $MY_ACCESS_TOKEN"

# Revoke one: it stops working at once
curl -s -X DELETE https://obsl.example.com/v1/auth/tokens/3f2a9c41b7e0 \
  -H "Authorization: Bearer $MY_ACCESS_TOKEN"
```

An admin (`obsl:admin`) lists everyone's with `GET /v1/auth/tokens?all=true`,
revokes anyone's by id, and revokes all of one person's at once, for example
when they leave: `DELETE /v1/auth/tokens?owner=<their sub>`.

## Good to know

- **Leaving the company.** OrionBelt is not told when someone leaves your
  identity provider. Their tokens work until they expire or an admin revokes
  them, so revoke them as part of offboarding.
- **Scopes are fixed.** A token keeps the scopes it was created with, even if
  the person's roles shrink later.
- **Sources that log in as the user** ([Warehouse Single Sign-On](warehouse-sso/index.md))
  refuse a BI token with 401 `SIGN_IN_REQUIRED`: a BI token is not your
  identity provider's token, so there is nothing to exchange for a warehouse
  token. Query those sources with a tool that signs you in.
- **Guessing.** After 10 failed attempts on one token id within a minute,
  that id is refused for a minute.
- **One store, one process.** Tokens live in one SQLite file
  (`TOKEN_STORE_PATH`), created with the first token. Put it on a persistent
  volume: on a platform without one (Cloud Run without a mounted volume), the
  tokens are gone after a restart. Several OrionBelt processes do not share it.

## Settings

| Variable | Default | Description |
|---|---|---|
| `TOKEN_STORE_PATH` | `./tokens/bi_tokens.sqlite` | The token store; empty turns BI tokens off |
| `TOKEN_DEFAULT_DAYS` | `90` | A new token's lifetime when the request names none |
| `TOKEN_MAX_DAYS` | `365` | The longest lifetime a request may ask for |
| `OIDC_TOKENS_CLIENT_ID` | (none) | The token page's own public client at the identity provider; unset: no page |
| `OIDC_TOKENS_SCOPES` | `openid` | What the token page's sign-in asks for |

## When something is refused

| Answer | What it means |
|---|---|
| 401 `invalid_token`, "BI token is not valid" | Unknown or wrong token, or too many failed attempts on its id |
| 401 `invalid_token`, "BI token expired" / "was revoked" | Create a new one |
| 403 `BI_TOKEN_NEEDS_SIGN_IN` | Creating a token needs your identity provider's sign-in, not an API key or a BI token |
| 403 `SCOPE_NOT_HELD` | You asked for a scope you do not hold |
| 422 `UNKNOWN_SCOPE` / `TOKEN_LIFETIME_TOO_LONG` | Use `obsl:read`, `obsl:write` or `obsl:admin`; ask for at most `TOKEN_MAX_DAYS` days |
| 404 on `/v1/auth/tokens` | `AUTH_MODE` is not `oidc`, or `TOKEN_STORE_PATH` is empty |

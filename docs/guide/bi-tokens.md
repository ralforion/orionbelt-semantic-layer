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
| Postgres protocol (Tableau, Power BI, psql), over TLS (`sslmode=require`) | Password = the token, any user name |
| Postgres protocol, without TLS | User name = the token's **id**, the 12 characters after `obsl_tok_` (`3f2a9c41b7e0`, or `tok_3f2a9c41b7e0`); password = the token. OrionBelt checks it with SCRAM against what it stored when the token was created, so the token never crosses the network. A user name of 12 lowercase hex characters, or `tok_` and them, is always taken as a token's id, so a service key logs in under another name, such as `obsl`. |

On the Postgres protocol the token is checked again before each query, so an
open connection ends once the token is revoked or expires.

Tool by tool (DBeaver, Tableau, Power BI, Metabase, Dremio, DuckDB), the
fields are in [Connecting BI tools](postgres-wire-bi-tools.md#logging-in).

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
  take a BI token only if it holds a warehouse sign-in: see
  [Data sources that log in as you](#data-sources-that-log-in-as-you).
- **Guessing.** After 10 failed attempts on one token id within a minute,
  that id is refused for a minute.
- **One store, one process.** Tokens live in one SQLite file
  (`TOKEN_STORE_PATH`), created with the first token. Put it on a persistent
  volume: on a platform without one (Cloud Run without a mounted volume), the
  tokens are gone after a restart. Several OrionBelt processes do not share it.

## Data sources that log in as you

A data source with `DB_<NAME>_AUTH=user` (Snowflake, Databricks, BigQuery)
queries the warehouse as the signed-in person, so the warehouse applies their
own grants and row-level security
([Warehouse Single Sign-On](warehouse-sso/index.md)). It needs the person's
identity provider token, and a BI token is not one.

So a BI token can hold a **warehouse sign-in**. When the token is created,
you sign in once more, this time as OrionBelt's own client. Your identity
provider then gives OrionBelt a long-lived refresh token, which OrionBelt
keeps encrypted with the BI token. At each query on such a source, OrionBelt
uses it to get a fresh token for you, and logs in to the warehouse as you.

A BI token without a warehouse sign-in still works on every other source.

### What the admin sets up

| What | Where |
|---|---|
| OrionBelt's own client (`OIDC_CLIENT_ID` and its secret) | At the identity provider: a confidential client that may use the authorization code flow, with the redirect URI `https://obsl.example.com/tokens/consent`, and that may ask for `offline_access` |
| Its tokens are for OrionBelt | They must carry `OIDC_AUDIENCE`, as a signed-in user's tokens do. Keycloak: an audience mapper. Entra ID: put the API's scope in `OIDC_CONSENT_SCOPES`. Okta: the authorization server's audience |
| A key for the refresh tokens | `TOKEN_STORE_KEY`, 32 random bytes in base64. Keep it outside the token store's directory |
| A data source that logs in as the user | `DB_<NAME>_AUTH=user`, set up as in [Warehouse Single Sign-On](warehouse-sso/index.md) |

Generate a key:

```bash
python3 -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

To change the key, put the new one first: `TOKEN_STORE_KEY=<new>,<old>`.
OrionBelt then encrypts with the new key and still reads values written
with the old one.

When something is missing, OrionBelt starts anyway and logs what a warehouse
sign-in still needs. Until then, these sources refuse BI tokens.

### Over the API

The token page does these steps for you. A client of its own:

1. `POST /v1/auth/tokens/consents`, signed in with your identity provider.
   The answer has a sign-in id (`consent`) and an `authorize_url`.
2. Open `authorize_url` in a browser window. After you sign in, the identity
   provider sends that window back to `/tokens/consent`, and OrionBelt keeps
   the refresh token for ten minutes.
3. `GET /v1/auth/tokens/consents/<consent>` until `status` is `finished`
   (or `failed`, with a `message`).
4. `POST /v1/auth/tokens` with `"consent": "<consent>"`. The new token's
   `warehouse_sign_in` is `true`.

### How long it lasts

At most as long as the BI token. Your identity provider can end it sooner:

| Identity provider | When it ends the sign-in |
|---|---|
| Keycloak | After 30 days without use (the realm's offline session idle time), or when the user is disabled |
| Okta | When the refresh token goes unused for the access policy rule's idle window: 7 days by default, so raise it for OrionBelt's client |
| Entra ID | When its policies say so, for example a Conditional Access sign-in frequency |

Logging out at the identity provider does **not** end it: it is an offline
sign-in. Revoking the BI token does. OrionBelt then also revokes the
refresh token at the identity provider, where the provider offers that
(Keycloak and Okta do; Entra ID has no such endpoint). Disabling the person
at the identity provider ends it too.

When it has ended, queries on these sources answer 401
`BI_TOKEN_SIGN_IN_ENDED`. Create a new BI token.

## Settings

| Variable | Default | Description |
|---|---|---|
| `TOKEN_STORE_PATH` | `./tokens/bi_tokens.sqlite` | The token store; empty turns BI tokens off |
| `TOKEN_DEFAULT_DAYS` | `90` | A new token's lifetime when the request names none |
| `TOKEN_MAX_DAYS` | `365` | The longest lifetime a request may ask for |
| `OIDC_TOKENS_CLIENT_ID` | (none) | The token page's own public client at the identity provider; unset: no page |
| `OIDC_TOKENS_SCOPES` | `openid` | What the token page's sign-in asks for |
| `TOKEN_STORE_KEY` or `TOKEN_STORE_KEY_FILE` | (none) | Keys for the warehouse sign-ins, base64, comma-separated, the first encrypts; unset: no warehouse sign-ins |
| `OIDC_CONSENT_SCOPES` | `OIDC_TOKENS_SCOPES` and `offline_access` | What the warehouse sign-in asks for |
| `OIDC_CONSENT_REDIRECT_URI` | `<API URL>/tokens/consent` | Where the identity provider sends the warehouse sign-in back; set it when OrionBelt sits behind a proxy that changes the address |
| `OIDC_REVOCATION_ENDPOINT` | from discovery | Where OrionBelt revokes a revoked token's refresh token at the identity provider |

## When something is refused

| Answer | What it means |
|---|---|
| 401 `invalid_token`, "BI token is not valid" | Unknown or wrong token, or too many failed attempts on its id |
| Postgres, "BI token is not valid" | Without TLS: the user name is not this token's id, or the token is wrong; or too many failed attempts on its id |
| Postgres, "SCRAM authentication failed" | Without TLS, a user name that is not a token's id takes an API key only: use the token's id as the user name |
| 401 `invalid_token`, "BI token expired" / "was revoked" | Create a new one. On Postgres without TLS, change the user name to the new token's id as well |
| 403 `BI_TOKEN_NEEDS_SIGN_IN` | Creating a token needs your identity provider's sign-in, not an API key or a BI token |
| 403 `SCOPE_NOT_HELD` | You asked for a scope you do not hold |
| 422 `UNKNOWN_SCOPE` / `TOKEN_LIFETIME_TOO_LONG` | Use `obsl:read`, `obsl:write` or `obsl:admin`; ask for at most `TOKEN_MAX_DAYS` days |
| 404 on `/v1/auth/tokens` | `AUTH_MODE` is not `oidc`, or `TOKEN_STORE_PATH` is empty |
| 401 `SIGN_IN_REQUIRED`, "this BI token holds no warehouse sign-in" | The source logs in as you, and the token was created without a warehouse sign-in. Create a new one |
| 401 `SIGN_IN_REQUIRED`, "BI tokens on this server hold no warehouse sign-in" | The server lacks what the message names, such as `TOKEN_STORE_KEY`. Ask your admin |
| 401 `BI_TOKEN_SIGN_IN_ENDED` | Your identity provider ended the warehouse sign-in. Create a new BI token |
| 404 `WAREHOUSE_SIGN_IN_OFF` | No source logs in as the user, or the server lacks a piece listed in [What the admin sets up](#what-the-admin-sets-up) |
| `CONSENT_NO_REFRESH_TOKEN` | The identity provider gave no refresh token. Allow `offline_access` for OrionBelt's client |
| `CONSENT_TOKEN_INVALID` | The token from OrionBelt's client is not for `OIDC_AUDIENCE`. Add the audience (see above) |
| `CONSENT_WRONG_USER` | Someone else signed in in the window than on the page. Sign in as yourself |
| 422 `CONSENT_NOT_FOUND` | The sign-in is unknown, someone else's, or older than ten minutes. Start again |

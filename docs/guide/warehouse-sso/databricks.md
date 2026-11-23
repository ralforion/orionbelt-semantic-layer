# Databricks

OrionBelt sends the user's access token to your workspace's token service,
which exchanges it for a Databricks token for the same user (Databricks OAuth
token federation). A **federation policy** decides which tokens Databricks
accepts.

Nothing changes at your identity provider, and OrionBelt needs no client
secret: any OpenID Connect provider works.

## What you need

| What | Why |
|---|---|
| Users signing in to OrionBelt with your identity provider | `AUTH_MODE=oidc`; see [OpenID Connect](../authentication.md#openid-connect) |
| Each user's Databricks username in a claim of their token, usually `email` | Databricks finds the user by it |
| An account admin of the Databricks account | Federation policies are account settings, not workspace settings |

## 1. Create the federation policy

In the account console (on AWS, `https://accounts.cloud.databricks.com`), open
**Security**, then the **Identity provider setup** tab, then **Federation
policies**, and choose **Create policy**:

| Field | What to enter | Example |
|---|---|---|
| Issuer URL | Your identity provider's issuer, exactly as the tokens' `iss` claim has it: the same value as `OIDC_ISSUER` | `https://login.microsoftonline.com/<tenant ID>/v2.0` |
| Audiences | The audience of the tokens users send OrionBelt: the same value as `OIDC_AUDIENCE` | `<OrionBelt's application ID>` |
| Subject claim | The claim that holds the user's Databricks username. Default `sub`. | `email` |
| Token signature validation | Leave empty to read the keys from the issuer's discovery document, or give a JWKS URI, or paste the keys as JWKS JSON (up to five keys) | |

The Databricks CLI does the same with `databricks account federation-policy
create`; see Databricks'
[federation policy guide](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-federation-policy).

!!! warning "A policy trusts the identity provider for every user"
    With an account-wide policy, any token the identity provider issues for
    that audience signs in as the Databricks user it names, admins included.
    Register only an identity provider you manage, and only the audience
    OrionBelt's tokens carry.

Databricks accepts tokens signed with RS256 or ES256. Entra ID, Okta and
Keycloak sign with RS256.

**A policy for one service principal** instead of the whole account needs the
exchange to name it: set `DB_<NAME>_OAUTH_CLIENT_ID` to the service
principal's client ID. Its queries then run as that service principal, not as
each user.

## 2. Configure the data source

```bash
AUTH_MODE=oidc
OIDC_ISSUER=https://login.microsoftonline.com/<tenant ID>/v2.0
OIDC_AUDIENCE=<OrionBelt's application ID>

OBSL_DATABASES=lakehouse
DB_LAKEHOUSE_TYPE=databricks
DB_LAKEHOUSE_AUTH=user
DB_LAKEHOUSE_DATABRICKS_SERVER_HOSTNAME=dbc-1234.cloud.databricks.com
DB_LAKEHOUSE_DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
```

The source needs no Databricks token: each user's comes from the exchange.

## 3. Check it

Run a query through OrionBelt as a signed-in user. In Databricks, **Query
History** lists it under that user's name. A user without access to a table
gets Databricks' own permission error, as in any other tool.

## Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| 403 with `TOKEN_INVALID (Ensure a valid federation policy has been configured)` | No federation policy matches the token's issuer and audience. The message shows the issuer and audience Databricks looked for. | Create the policy, or correct its **Issuer URL** and **Audiences** to the values the message shows. The issuer must match exactly, trailing slash included. |
| 403, and the user exists in your identity provider | The subject claim's value is not a Databricks username, or the user has no access to the workspace. | Check the **Subject claim** of the policy and the user's Databricks username. Decode one token (any JWT decoder) to see the claim's value. |
| 401 `SIGN_IN_REQUIRED` | The caller used an API key, or no authentication. | Call with the user's access token. |
| 503 `DATA_SOURCE_SIGN_IN_UNAVAILABLE` | OrionBelt could not reach the workspace's token service. | The network from OrionBelt to the workspace, and `DB_<NAME>_DATABRICKS_SERVER_HOSTNAME`. |

Exchanged tokens expire with the user's token; OrionBelt exchanges again with
the token of the next request.

# Try It with a Local Keycloak

You can try single sign-on to Snowflake, Databricks and BigQuery before you
change anything at your company's identity provider: a Keycloak on your own
machine plays the identity provider, and the warehouses trust its key
directly. Nothing has to be reachable from the internet, because each
warehouse is given the key itself instead of a URL to fetch it from.

You need Docker, and admin rights in the warehouses you try: Snowflake
`ACCOUNTADMIN`; a Databricks account admin, and a Databricks user to sign in
as; for BigQuery, `roles/iam.workforcePoolAdmin` in a Google Cloud
organization and a project inside it.

!!! warning "Remove the trust when you are done"
    While the warehouses trust this Keycloak's key, anyone who holds the key
    can sign in. Through a Databricks account federation policy that means
    **as any Databricks user in the account**. Delete the policy after each
    test; switch the Snowflake integration and the BigQuery workforce
    provider off between tests ([clean up](#7-clean-up)).

## 1. Start Keycloak

```bash
docker run -d --name obsl-keycloak -p 127.0.0.1:18080:8080 \
  -v obsl-keycloak-data:/opt/keycloak/data \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KC_BOOTSTRAP_ADMIN_PASSWORD=<an admin password> \
  quay.io/keycloak/keycloak:26.8.2 start-dev \
  --hostname=https://keycloak.obsl.test \
  --hostname-backchannel-dynamic=true
```

- `--hostname` makes the tokens' issuer `https://keycloak.obsl.test/realms/<realm>`.
  The name never has to resolve: nobody fetches anything from it.
- `--hostname-backchannel-dynamic` lets you and OrionBelt reach Keycloak at
  `http://127.0.0.1:18080`: its token endpoint and its admin API.
- The admin console is not used: it signs in through the `--hostname`
  address, which does not resolve. Step 2 configures Keycloak through its
  admin API instead.
- The volume keeps the realm **and its signing key**. Recreating the
  container without it creates a new key, and the warehouses would need the
  new one.

## 2. Configure the realm

Set these, then run the script below. It needs `curl` and `jq`, and prints
nothing when every call succeeds.

```bash
KC=http://127.0.0.1:18080
ADMIN_PASSWORD='<the admin password from step 1>'
ROLE=<the Snowflake role to test with>           # e.g. ANALYST
CLIENT_SECRET=$(openssl rand -hex 24)              # OrionBelt's client secret
USER_PASSWORD='<a password for the test users>'
DBX_USER=<your Databricks username>
```

```bash
ADMIN=$(curl -s $KC/realms/master/protocol/openid-connect/token \
  -d grant_type=password -d client_id=admin-cli \
  -d username=admin --data-urlencode "password=$ADMIN_PASSWORD" | jq -r .access_token)
kc() { curl -sf -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' "$@"; }
R=$KC/admin/realms

kc -X POST $R -d '{"realm": "obsl", "displayName": "OrionBelt® Semantic Layer", "enabled": true}'

# obsl:read lets a user query OrionBelt; session:role:<ROLE> carries the Snowflake role.
kc -X POST $R/obsl/client-scopes -d '{"name": "obsl:read", "protocol": "openid-connect",
  "attributes": {"include.in.token.scope": "true"}}'
kc -X POST $R/obsl/client-scopes -d "$(jq -n --arg role "$ROLE" '{
  name: "session:role:\($role)", protocol: "openid-connect",
  protocolMappers: [
    {name: "aud-snowflake", protocol: "openid-connect", protocolMapper: "oidc-audience-mapper",
     config: {"included.client.audience": "snowflake", "access.token.claim": "true"}},
    {name: "scp", protocol: "openid-connect", protocolMapper: "oidc-hardcoded-claim-mapper",
     config: {"claim.name": "scp", "claim.value": "[\"session:role:\($role)\"]",
              "jsonType.label": "JSON", "access.token.claim": "true"}}]}')"

kc -X POST $R/obsl/clients -d '{"clientId": "snowflake"}'
kc -X POST $R/obsl/clients -d "$(jq -n --arg secret "$CLIENT_SECRET" --arg role "$ROLE" '{
  clientId: "obsl", publicClient: false, secret: $secret, standardFlowEnabled: false,
  attributes: {"standard.token.exchange.enabled": "true"},
  defaultClientScopes: ["basic", "email", "profile"],
  optionalClientScopes: ["session:role:\($role)"]}')"
kc -X POST $R/obsl/clients -d '{"clientId": "obsl-login", "publicClient": true,
  "standardFlowEnabled": false, "directAccessGrantsEnabled": true,
  "defaultClientScopes": ["basic", "email", "profile", "obsl:read"],
  "protocolMappers": [{"name": "aud-obsl", "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {"included.client.audience": "obsl", "access.token.claim": "true"}}]}'

for user in "alice alice@obsl.test" "dbx $DBX_USER"; do
  set -- $user
  kc -X POST $R/obsl/users -d "$(jq -n --arg u "$1" --arg e "$2" --arg p "$USER_PASSWORD" '{
    username: $u, email: $e, emailVerified: true, firstName: $u, lastName: "Test",
    enabled: true, credentials: [{type: "password", value: $p, temporary: false}]}')"
done
```

What it creates in realm `obsl`:

| Object | Why |
|---|---|
| Client scope `obsl:read` | The [OrionBelt scope](../authentication.md#scopes) that lets a user query. Without it OrionBelt answers 403 `AUTH_INSUFFICIENT_SCOPE`. |
| Client scope `session:role:<ROLE>` | Adds Snowflake as an audience and the `scp` claim Snowflake reads the role from. |
| Client `snowflake` | Only names Snowflake as an audience. |
| Client `obsl` (OrionBelt) | Confidential, with standard token exchange on. Its default scopes give the exchanged token `sub` and `email`, which Snowflake maps users by; the role scope is optional, asked for by `DB_<NAME>_OAUTH_SCOPE`. |
| Client `obsl-login` (this test only) | Signs users in with a password. Its tokens name `obsl` in `aud`, which the exchange requires, and carry `obsl:read`. |
| Users `alice` and `dbx` | `alice@obsl.test` for Snowflake; `dbx` has your Databricks username as its email. |

## 3. Give the warehouses the key

Keycloak publishes the key in two forms: a base64 public key for Snowflake,
and a key set (JWKS) for Databricks, BigQuery and OrionBelt. Save both:

```bash
curl -s $KC/realms/obsl | jq -r .public_key > snowflake_public_key.txt
curl -s $KC/realms/obsl/protocol/openid-connect/certs \
  | jq '{keys: [.keys[] | select(.use == "sig" and .alg == "RS256") | {kid, kty, alg, use, n, e}]}' > jwks.json
```

**Snowflake**, as `ACCOUNTADMIN` for the integration and `SECURITYADMIN` for
the user:

```sql
USE ROLE ACCOUNTADMIN;
CREATE SECURITY INTEGRATION obsl_keycloak_test
  TYPE = EXTERNAL_OAUTH
  ENABLED = TRUE
  EXTERNAL_OAUTH_TYPE = CUSTOM
  EXTERNAL_OAUTH_ISSUER = 'https://keycloak.obsl.test/realms/obsl'
  EXTERNAL_OAUTH_RSA_PUBLIC_KEY = '<the base64 public key>'
  EXTERNAL_OAUTH_AUDIENCE_LIST = ('snowflake')
  EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'email'
  EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME'
  EXTERNAL_OAUTH_ALLOWED_ROLES_LIST = ('<ROLE>');

USE ROLE SECURITYADMIN;
CREATE USER obsl_sso_test LOGIN_NAME = 'alice@obsl.test' DEFAULT_ROLE = <ROLE> TYPE = PERSON;
GRANT ROLE <ROLE> TO USER obsl_sso_test;
```

**Databricks**, in the account console: a federation policy with the issuer
`https://keycloak.obsl.test/realms/obsl`, the audience `obsl`, the subject
claim `email`, and the contents of `jwks.json` as **JWKS JSON**. See
[Databricks](databricks.md#1-create-the-federation-policy).

**BigQuery**, a workforce pool in your organization whose provider trusts the
key, and `alice` allowed to run jobs in a project inside the organization:

```bash
gcloud iam workforce-pools create obsl-sso-test \
  --organization=<organization ID> --location=global --session-duration=3600s

gcloud iam workforce-pools providers create-oidc keycloak \
  --workforce-pool=obsl-sso-test --location=global \
  --issuer-uri=https://keycloak.obsl.test/realms/obsl \
  --client-id=obsl \
  --attribute-mapping="google.subject=assertion.email" \
  --jwk-json-path=jwks.json \
  --web-sso-response-type=id-token \
  --web-sso-assertion-claims-behavior=only-id-token-claims

ALICE='principal://iam.googleapis.com/locations/global/workforcePools/obsl-sso-test/subject/alice@obsl.test'
gcloud projects add-iam-policy-binding <project> \
  --role=roles/serviceusage.serviceUsageConsumer --member="$ALICE"
gcloud projects add-iam-policy-binding <project> \
  --role=roles/bigquery.jobUser --member="$ALICE"
```

The project needs the BigQuery and Security Token Service APIs on
(`gcloud services enable bigquery.googleapis.com sts.googleapis.com --project=<project>`).
A new grant takes a minute or two to apply. See [BigQuery](bigquery.md).

## 4. Configure OrionBelt

```bash
QUERY_EXECUTE=true
AUTH_MODE=oidc
OIDC_ISSUER=https://keycloak.obsl.test/realms/obsl
OIDC_AUDIENCE=obsl
OIDC_JWKS_FILE=/path/to/jwks.json    # from step 3
OIDC_TOKEN_ENDPOINT=http://127.0.0.1:18080/realms/obsl/protocol/openid-connect/token
OIDC_CLIENT_ID=obsl
OIDC_CLIENT_SECRET=<$CLIENT_SECRET from step 2>

OBSL_DATABASES=sf,dbx,bq

DB_SF_TYPE=snowflake
DB_SF_AUTH=user
DB_SF_OAUTH_EXCHANGE=token_exchange
DB_SF_OAUTH_SCOPE=session:role:<ROLE>
DB_SF_OAUTH_AUDIENCE=snowflake
DB_SF_SNOWFLAKE_ACCOUNT=<account>
DB_SF_SNOWFLAKE_WAREHOUSE=<warehouse>
DB_SF_SNOWFLAKE_DATABASE=<database>

DB_DBX_TYPE=databricks
DB_DBX_AUTH=user
DB_DBX_DATABRICKS_SERVER_HOSTNAME=<workspace host>
DB_DBX_DATABRICKS_HTTP_PATH=<SQL warehouse HTTP path>

DB_BQ_TYPE=bigquery
DB_BQ_AUTH=user
DB_BQ_BIGQUERY_PROJECT=<project>
DB_BQ_WORKFORCE_PROVIDER=//iam.googleapis.com/locations/global/workforcePools/obsl-sso-test/providers/keycloak
```

`OIDC_JWKS_FILE` and `OIDC_TOKEN_ENDPOINT` are needed because the issuer's
address does not resolve, so OrionBelt cannot read its discovery document.

## 5. Query as a user

With a model loaded (`MODEL_FILES`) whose `settings.defaultDataSource` is `sf`,
`dbx` or `bq`, get a user's token from Keycloak and call OrionBelt with it. The
token carries `obsl:read`, the scope OrionBelt checks:

```bash
TOKEN=$(curl -s http://127.0.0.1:18080/realms/obsl/protocol/openid-connect/token \
  -d grant_type=password -d client_id=obsl-login \
  -d username=alice --data-urlencode "password=$USER_PASSWORD" | jq -r .access_token)

curl -s http://localhost:8000/v1/query/execute \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"select": {"dimensions": ["..."], "measures": ["..."]}}'
```

In the warehouse's **Query History**, the query runs as that user:
`OBSL_SSO_TEST` with your role in Snowflake, your own user in Databricks, and
`principal://iam.googleapis.com/locations/global/workforcePools/obsl-sso-test/subject/alice@obsl.test`
in BigQuery.

## 6. A BI token that logs in as the user

A BI tool cannot sign in, so it holds a [BI token](../bi-tokens.md). To reach
these sources, the token needs a **warehouse sign-in**: when it is created,
the user signs in once more as OrionBelt's own client, and Keycloak gives
OrionBelt an offline refresh token for them (see
[Data sources that log in as you](../bi-tokens.md#data-sources-that-log-in-as-you)).

Let client `obsl` sign users in. With `kc` and `R` from step 2 (get `ADMIN`
again first: the admin token lasts a minute):

```bash
OBSL_URL=http://localhost:8000
ID=$(kc "$R/obsl/clients?clientId=obsl" | jq -r '.[0].id')
kc "$R/obsl/clients/$ID" | jq --arg uri "$OBSL_URL/tokens/consent" '
  .standardFlowEnabled = true | .redirectUris = [$uri]
  | .attributes["pkce.code.challenge.method"] = "S256"' | kc -X PUT "$R/obsl/clients/$ID" -d @-
kc -X POST "$R/obsl/clients/$ID/protocol-mappers/models" -d '{"name": "aud-obsl",
  "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
  "config": {"included.client.audience": "obsl", "access.token.claim": "true"}}'
OFFLINE=$(kc "$R/obsl/client-scopes" | jq -r '.[] | select(.name == "offline_access") | .id')
kc -X PUT "$R/obsl/clients/$ID/optional-client-scopes/$OFFLINE"
```

| Change on client `obsl` | Why |
|---|---|
| Standard flow, with PKCE, and the redirect URI `<OrionBelt>/tokens/consent` | The user signs in as `obsl`, and Keycloak sends the code back to OrionBelt |
| Audience mapper `obsl` | The tokens OrionBelt gets for the user are for OrionBelt (`OIDC_AUDIENCE`), as those of `obsl-login` are |
| Optional client scope `offline_access` | The refresh token outlives the user's Keycloak session: it ends after 30 days unused (the realm's default), when the user is disabled, or when it is revoked |

Keep the default client scope `roles` off `obsl`, as step 2 does: it would
add `account` to the tokens' audience.

Add to OrionBelt's settings from step 4, and restart it:

```bash
OIDC_AUTHORIZATION_ENDPOINT=https://keycloak.obsl.test/realms/obsl/protocol/openid-connect/auth
OIDC_REVOCATION_ENDPOINT=http://127.0.0.1:18080/realms/obsl/protocol/openid-connect/revoke
TOKEN_STORE_KEY=<python3 -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())">
```

OrionBelt's log then says *BI tokens created on /tokens take a warehouse
sign-in along*.

With a real identity provider, the [token page](../bi-tokens.md#on-the-token-page)
opens the sign-in in a pop-up. Here a browser cannot finish it: Keycloak's
sign-in form posts to the `--hostname` address, which does not resolve
(step 1). This script does the pop-up's part over the API, and prints a BI
token that holds a warehouse sign-in. It needs `httpx`, which comes with
OrionBelt:

```python
# USER_PASSWORD='<the test users' password>' python bi_token_sign_in.py
import html
import os
import re

import httpx

KC, FRONT, OBSL = "http://127.0.0.1:18080", "https://keycloak.obsl.test", "http://localhost:8000"
password = os.environ["USER_PASSWORD"]

# Alice signs in to OrionBelt (on the token page, through Keycloak).
token = httpx.post(f"{KC}/realms/obsl/protocol/openid-connect/token", data={
    "grant_type": "password", "client_id": "obsl-login",
    "username": "alice", "password": password}).json()["access_token"]
alice = {"Authorization": f"Bearer {token}"}

# OrionBelt starts the warehouse sign-in.
started = httpx.post(f"{OBSL}/v1/auth/tokens/consents", headers=alice).json()

# Alice signs in at Keycloak as `obsl`: the pop-up's part. Keycloak marks its
# cookies Secure, which httpx keeps off plain http, so they are passed along.
page = httpx.get(started["authorize_url"].replace(FRONT, KC))
form = html.unescape(re.search(r'action="([^"]+)"', page.text).group(1)).replace(FRONT, KC)
cookies = "; ".join(c.split(";", 1)[0] for c in page.headers.get_list("set-cookie"))
signed_in = httpx.post(form, headers={"Cookie": cookies},
                       data={"username": "alice", "password": password})

# Keycloak sends the code back to OrionBelt, which keeps the refresh token.
httpx.get(signed_in.headers["location"])

# The new BI token takes the sign-in along.
created = httpx.post(f"{OBSL}/v1/auth/tokens", headers=alice,
                     json={"label": "Tableau", "consent": started["consent"]}).json()
print("warehouse sign-in:", created["warehouse_sign_in"])
print(created["token"])
```

Use that token as a BI tool would: step 5's request with
`Authorization: Bearer <the BI token>`, or the token as the password over
the [Postgres wire protocol or Flight SQL](../bi-tokens.md#use-it-in-a-tool).
The query runs as Alice, `OBSL_SSO_TEST` in Snowflake's **Query History**.

Revoking the token (`DELETE /v1/auth/tokens/<id>`) ends its sign-in, and
OrionBelt revokes the refresh token at Keycloak too. Disabling Alice in
Keycloak ends it as well. Logging her out does not: it is an offline
session.

## 7. Clean up

```sql
-- Snowflake: switch the integration off between tests...
USE ROLE ACCOUNTADMIN;
ALTER SECURITY INTEGRATION obsl_keycloak_test SET ENABLED = FALSE;

-- ...or remove everything.
USE ROLE SECURITYADMIN; DROP USER obsl_sso_test;
USE ROLE ACCOUNTADMIN;  DROP INTEGRATION obsl_keycloak_test;
```

In Databricks, delete the federation policy. In Google Cloud, switch the
workforce provider off (`--no-disabled` switches it on again), or delete the
pool:

```bash
gcloud iam workforce-pools providers update-oidc keycloak \
  --workforce-pool=obsl-sso-test --location=global --disabled
gcloud iam workforce-pools delete obsl-sso-test --location=global
```

Then stop Keycloak
(`docker stop obsl-keycloak`), or remove it with its key:
`docker rm -f obsl-keycloak && docker volume rm obsl-keycloak-data`.

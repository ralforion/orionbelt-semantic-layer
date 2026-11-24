# Try It with a Local Keycloak

You can try single sign-on to Snowflake and Databricks before you change
anything at your company's identity provider: a Keycloak on your own machine
plays the identity provider, and both warehouses trust its key directly.
Nothing has to be reachable from the internet, because Snowflake and
Databricks are given the key itself instead of a URL to fetch it from.

You need Docker, admin rights in the warehouses you try (Snowflake
`ACCOUNTADMIN`, Databricks account admin), and a Databricks user to sign in as.

!!! warning "Remove the trust when you are done"
    While the warehouses trust this Keycloak's key, anyone who holds the key
    can sign in. Through a Databricks account federation policy that means
    **as any Databricks user in the account**. Delete the policy after each
    test; switch the Snowflake integration off between tests
    ([clean up](#6-clean-up)).

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

kc -X POST $R -d '{"realm": "obsl", "enabled": true}'

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
and a key set (JWKS) for Databricks and OrionBelt. Save both:

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

## 4. Configure OrionBelt

```bash
AUTH_MODE=oidc
OIDC_ISSUER=https://keycloak.obsl.test/realms/obsl
OIDC_AUDIENCE=obsl
OIDC_JWKS_FILE=/path/to/jwks.json    # from step 3
OIDC_TOKEN_ENDPOINT=http://127.0.0.1:18080/realms/obsl/protocol/openid-connect/token
OIDC_CLIENT_ID=obsl
OIDC_CLIENT_SECRET=<$CLIENT_SECRET from step 2>

OBSL_DATABASES=sf,dbx

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
```

`OIDC_JWKS_FILE` and `OIDC_TOKEN_ENDPOINT` are needed because the issuer's
address does not resolve, so OrionBelt cannot read its discovery document.

## 5. Query as a user

With a model loaded (`MODEL_FILES`) whose `settings.defaultDataSource` is `sf`
or `dbx`, get a user's token from Keycloak and call OrionBelt with it. The
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
`OBSL_SSO_TEST` with your role in Snowflake, your own user in Databricks.

## 6. Clean up

```sql
-- Snowflake: switch the integration off between tests...
USE ROLE ACCOUNTADMIN;
ALTER SECURITY INTEGRATION obsl_keycloak_test SET ENABLED = FALSE;

-- ...or remove everything.
USE ROLE SECURITYADMIN; DROP USER obsl_sso_test;
USE ROLE ACCOUNTADMIN;  DROP INTEGRATION obsl_keycloak_test;
```

In Databricks, delete the federation policy. Then stop Keycloak
(`docker stop obsl-keycloak`), or remove it with its key:
`docker rm -f obsl-keycloak && docker volume rm obsl-keycloak-data`.

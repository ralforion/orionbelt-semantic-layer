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
  --hostname-admin=http://127.0.0.1:18080 \
  --hostname-backchannel-dynamic=true
```

- `--hostname` makes the tokens' issuer `https://keycloak.obsl.test/realms/<realm>`.
  The name never has to resolve: nobody fetches anything from it.
- `--hostname-admin` keeps the admin console at `http://127.0.0.1:18080/admin`.
- `--hostname-backchannel-dynamic` lets OrionBelt reach the token endpoint at
  `http://127.0.0.1:18080`.
- The volume keeps the realm **and its signing key**. Recreating the
  container without it creates a new key, and the warehouses would need the
  new one.

## 2. Configure the realm

In the admin console, create a realm `obsl`, then:

| Object | Settings |
|---|---|
| Client `snowflake` | Default settings. It only names Snowflake as an audience. |
| Client `obsl` (OrionBelt) | **Client authentication** on; **Capability config**: **Standard token exchange** on, **Standard flow** off. Copy its secret from **Credentials**. |
| Client `obsl-login` (for this test) | **Client authentication** off; **Direct access grants** on, so you can get a user's token with a password. In its dedicated client scope, an **Audience** mapper: *Included Client Audience* `obsl`, *Add to access token* on. |
| Client scope `session:role:<ROLE>` | `<ROLE>` is the Snowflake role to test with. Mappers: **Audience** (*Included Client Audience* `snowflake`, *Add to access token* on) and **Hardcoded claim** (*Token Claim Name* `scp`, *Claim value* `["session:role:<ROLE>"]`, *Claim JSON Type* `JSON`, *Add to access token* on). Then, on client `obsl`, add it as an **Optional** client scope. |
| User for Snowflake | Email (for example `alice@obsl.test`) marked verified, first and last name, a password that is not temporary. |
| User for Databricks | The same, with your Databricks username as the email. |

## 3. Give the warehouses the key

Keycloak publishes the key in two forms:

| Form | Where | Used by |
|---|---|---|
| Base64 public key | the `public_key` field of `http://127.0.0.1:18080/realms/obsl` | Snowflake |
| JWKS | `http://127.0.0.1:18080/realms/obsl/protocol/openid-connect/certs`: keep the key with `"use": "sig"` and `"alg": "RS256"` | Databricks, and OrionBelt (`OIDC_JWKS_FILE`) |

Save the JWKS, reduced to that key, as `jwks.json`.

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
OIDC_JWKS_FILE=/path/to/jwks.json
OIDC_TOKEN_ENDPOINT=http://127.0.0.1:18080/realms/obsl/protocol/openid-connect/token
OIDC_CLIENT_ID=obsl
OIDC_CLIENT_SECRET=<client obsl's secret>

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
or `dbx`, get a user's token from Keycloak and call OrionBelt with it:

```bash
TOKEN=$(curl -s http://127.0.0.1:18080/realms/obsl/protocol/openid-connect/token \
  -d grant_type=password -d client_id=obsl-login \
  -d username=alice -d password=<alice's password> | jq -r .access_token)

curl -s http://localhost:8000/v1/query/execute \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": {"select": {"dimensions": ["..."], "measures": ["..."]}}}'
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

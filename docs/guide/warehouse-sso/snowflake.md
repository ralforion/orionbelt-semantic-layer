# Snowflake

Snowflake accepts tokens that your identity provider issues **for Snowflake**
(Snowflake External OAuth). So OrionBelt asks the identity provider for one, on
the user's behalf, as a client of its own, and connects with it. The token's
scope carries the Snowflake role.

| Your identity provider | How OrionBelt asks it | `DB_<NAME>_OAUTH_EXCHANGE` |
|---|---|---|
| [Entra ID](#entra-id) | Entra's on-behalf-of flow | `entra_obo` |
| [Okta](#okta), [Keycloak](#keycloak), others with the standard token exchange (RFC 8693) | Token exchange | `token_exchange` |

## What you need

| Where | What | Who can do it |
|---|---|---|
| Identity provider | OrionBelt's own client, with a secret; the Snowflake scopes (one per role) and permission for OrionBelt's client to ask for them | Identity provider admin |
| Snowflake | A security integration that trusts the identity provider | `ACCOUNTADMIN`: by default the only role with `CREATE INTEGRATION` |
| Snowflake | Users whose login name matches a claim of the tokens, granted their roles | `SECURITYADMIN` is enough |
| OrionBelt | `OIDC_CLIENT_ID` and its secret; the data source | |

The queries themselves run with the role the token carries, never with
`ACCOUNTADMIN`: Snowflake blocks `ACCOUNTADMIN`, `ORGADMIN`, `GLOBALORGADMIN`
and `SECURITYADMIN` for External OAuth by default.

## 1. At the identity provider

### Entra ID

Two app registrations:

| App registration | What it needs |
|---|---|
| **Snowflake** | Under **Expose an API**: an Application ID URI (such as `api://<Snowflake app ID>`) and one scope per Snowflake role, `session:scope:<role>`, or `session:role-any` to let the user's default role apply. See Snowflake's [guide for Entra ID](https://docs.snowflake.com/en/user-guide/oauth-azure). |
| **OrionBelt** | The one users sign in to OrionBelt with: `OIDC_AUDIENCE` is its application ID (see the [identity provider notes](../authentication.md#identity-provider-notes)). Under **Certificates & secrets**, a client secret. Under **API permissions**, the delegated permission to the Snowflake scopes, with admin consent granted. |

The on-behalf-of flow only accepts a user's token issued for the app that
asks, which is why OrionBelt asks as the same app users sign in to.

### Okta

| Okta object | What it needs |
|---|---|
| A custom authorization server for Snowflake | Scopes named after the roles, such as `session:role:ANALYST`; its audience is what Snowflake trusts |
| An **API Services** app for OrionBelt | The **Token Exchange** grant (under **Advanced** in the grant types). Its client ID and secret are `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`. |
| An access policy on that authorization server | A rule that lets OrionBelt's app ask for the role scopes |

`DB_<NAME>_OAUTH_AUDIENCE` is that authorization server's audience. See Okta's
[token exchange guide](https://developer.okta.com/docs/guides/set-up-token-exchange/main/)
and Snowflake's [guide for Okta](https://docs.snowflake.com/en/user-guide/oauth-okta).

### Keycloak

Keycloak 26.2 or later, in the realm users sign in to:

| Keycloak object | What it needs |
|---|---|
| Client `snowflake` | Exists only to name Snowflake as an audience. |
| Client for OrionBelt (here `obsl`) | **Client authentication** on (a confidential client); in **Capability config**, **Standard token exchange** on. Its client ID and secret are `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET`. Keep the default client scopes `basic`, `email` and `profile` on it: they put `sub` and `email` in the exchanged token. |
| Users' tokens | Must name `obsl` in `aud`, or Keycloak refuses the exchange: on the client users sign in with, an **Audience** mapper with *Included Client Audience* `obsl`. `OIDC_AUDIENCE` is then `obsl`. They also need an [OrionBelt scope](../authentication.md#scopes), such as a client scope `obsl:read`. |
| Client scope per role, such as `session:role:ANALYST` | Two mappers: **Audience** (*Included Client Audience* `snowflake`) and **Hardcoded claim** (*Token Claim Name* `scp`, *Claim value* `["session:role:ANALYST"]`, *Claim JSON Type* `JSON`). Snowflake reads the role from `scp`; Keycloak's own `scope` claim lists other scopes with it. Add the scope to `obsl` as an **Optional** client scope. |

`DB_<NAME>_OAUTH_SCOPE` names that client scope, and
`DB_<NAME>_OAUTH_AUDIENCE` is `snowflake`.

## 2. The security integration

The integration tells Snowflake which tokens to trust and how to find the
Snowflake user in them. Run it as `ACCOUNTADMIN`.

=== "Entra ID"

    ```sql
    CREATE SECURITY INTEGRATION entra_orionbelt
      TYPE = EXTERNAL_OAUTH
      ENABLED = TRUE
      EXTERNAL_OAUTH_TYPE = AZURE
      EXTERNAL_OAUTH_ISSUER = 'https://sts.windows.net/<tenant ID>/'
      EXTERNAL_OAUTH_JWS_KEYS_URL = 'https://login.microsoftonline.com/<tenant ID>/discovery/v2.0/keys'
      EXTERNAL_OAUTH_AUDIENCE_LIST = ('api://<Snowflake app ID>')
      EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'upn'
      EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME'
      EXTERNAL_OAUTH_ANY_ROLE_MODE = 'ENABLE';
    ```

    `EXTERNAL_OAUTH_ANY_ROLE_MODE` is for the `session:role-any` scope; leave
    it out when every scope names a role. The issuer must match the `iss` of the tokens Entra issues for Snowflake's
    app: `https://sts.windows.net/<tenant ID>/` for its default v1 tokens.

=== "Okta"

    ```sql
    CREATE SECURITY INTEGRATION okta_orionbelt
      TYPE = EXTERNAL_OAUTH
      ENABLED = TRUE
      EXTERNAL_OAUTH_TYPE = OKTA
      EXTERNAL_OAUTH_ISSUER = 'https://<Okta domain>/oauth2/<authorization server ID>'
      EXTERNAL_OAUTH_JWS_KEYS_URL = 'https://<Okta domain>/oauth2/<authorization server ID>/v1/keys'
      EXTERNAL_OAUTH_AUDIENCE_LIST = ('<authorization server audience>')
      EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'sub'
      EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME';
    ```

=== "Keycloak"

    ```sql
    CREATE SECURITY INTEGRATION keycloak_orionbelt
      TYPE = EXTERNAL_OAUTH
      ENABLED = TRUE
      EXTERNAL_OAUTH_TYPE = CUSTOM
      EXTERNAL_OAUTH_ISSUER = 'https://keycloak.example.com/realms/<realm>'
      EXTERNAL_OAUTH_JWS_KEYS_URL = 'https://keycloak.example.com/realms/<realm>/protocol/openid-connect/certs'
      EXTERNAL_OAUTH_AUDIENCE_LIST = ('snowflake')
      EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'email'
      EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME';
    ```

    When Snowflake cannot reach Keycloak, give its key instead of the URL:
    `EXTERNAL_OAUTH_RSA_PUBLIC_KEY = '<public key>'`, the realm's RSA public
    key, base64, without the `BEGIN` and `END` lines (the `public_key` field of
    `https://<host>/realms/<realm>`).

Useful additions:

| Parameter | What it does |
|---|---|
| `EXTERNAL_OAUTH_ALLOWED_ROLES_LIST = ('ANALYST', ...)` | Only these roles may be used through the integration. |
| `EXTERNAL_OAUTH_ANY_ROLE_MODE = 'ENABLE'` | Needed for tokens with `session:role-any`. |
| `ENABLED = FALSE` | Turns the integration off without dropping it: `ALTER SECURITY INTEGRATION <name> SET ENABLED = FALSE`. |

## 3. Users and roles

Each user needs a Snowflake user whose login name is the value of the mapping
claim, and the roles their scopes name. As `SECURITYADMIN`:

```sql
CREATE USER jane LOGIN_NAME = 'jane@example.com' DEFAULT_ROLE = ANALYST TYPE = PERSON;
GRANT ROLE ANALYST TO USER jane;
```

The user needs no password: the token is how they sign in through OrionBelt.

**Which role a query runs with:** a role scope (`session:role:ANALYST`,
`session:scope:analyst`) fixes it. With `session:role-any`,
`DB_<NAME>_SNOWFLAKE_ROLE` picks it, or else the user's default role applies.

## 4. Configure the data source

=== "Entra ID"

    ```bash
    QUERY_EXECUTE=true
    AUTH_MODE=oidc
    OIDC_ISSUER=https://login.microsoftonline.com/<tenant ID>/v2.0
    OIDC_AUDIENCE=<OrionBelt's application ID>
    OIDC_CLIENT_ID=<OrionBelt's application ID>
    OIDC_CLIENT_SECRET_FILE=/run/secrets/obsl-client-secret

    OBSL_DATABASES=warehouse
    DB_WAREHOUSE_TYPE=snowflake
    DB_WAREHOUSE_AUTH=user
    DB_WAREHOUSE_OAUTH_EXCHANGE=entra_obo
    DB_WAREHOUSE_OAUTH_SCOPE=api://<Snowflake app ID>/session:role-any
    DB_WAREHOUSE_SNOWFLAKE_ACCOUNT=xy12345.eu-west-1
    DB_WAREHOUSE_SNOWFLAKE_WAREHOUSE=BI_WH
    DB_WAREHOUSE_SNOWFLAKE_DATABASE=SALES
    ```

=== "Keycloak"

    ```bash
    QUERY_EXECUTE=true
    AUTH_MODE=oidc
    OIDC_ISSUER=https://keycloak.example.com/realms/<realm>
    OIDC_AUDIENCE=obsl
    OIDC_CLIENT_ID=obsl
    OIDC_CLIENT_SECRET_FILE=/run/secrets/obsl-client-secret

    OBSL_DATABASES=warehouse
    DB_WAREHOUSE_TYPE=snowflake
    DB_WAREHOUSE_AUTH=user
    DB_WAREHOUSE_OAUTH_EXCHANGE=token_exchange
    DB_WAREHOUSE_OAUTH_SCOPE=session:role:ANALYST
    DB_WAREHOUSE_OAUTH_AUDIENCE=snowflake
    DB_WAREHOUSE_SNOWFLAKE_ACCOUNT=xy12345.eu-west-1
    DB_WAREHOUSE_SNOWFLAKE_WAREHOUSE=BI_WH
    DB_WAREHOUSE_SNOWFLAKE_DATABASE=SALES
    ```

For Okta it is the same, with `OIDC_ISSUER` and `OIDC_AUDIENCE` of the
authorization server users sign in to OrionBelt with, `OIDC_CLIENT_ID` and the
secret of OrionBelt's API Services app, and `DB_<NAME>_OAUTH_AUDIENCE` the
audience of the authorization server Snowflake trusts.

OrionBelt asks at `OIDC_TOKEN_ENDPOINT`, or at the token endpoint the
issuer's discovery document names. For the token exchange its client
authenticates with HTTP Basic, with the id and secret form-encoded as RFC 6749
requires; Entra's on-behalf-of flow sends the secret in the request.

A `SNOWFLAKE_USER` or `SNOWFLAKE_PASSWORD` set for the source is not sent: the
token says who connects.

## 5. Check it

Run a query through OrionBelt as a signed-in user. In Snowflake, **Query
History** lists it under that user, with the role from the token.

## Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| OrionBelt refuses to start, naming `OIDC_CLIENT_ID` | A Snowflake source logs in as the user, and OrionBelt has no client at the identity provider. | Set `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` or `OIDC_CLIENT_SECRET_FILE`. |
| OrionBelt refuses to start, naming `OIDC_TOKEN_ENDPOINT` | `OIDC_JWKS_URI` or `OIDC_JWKS_FILE` skips the discovery document that names the token endpoint. | Set `OIDC_TOKEN_ENDPOINT`. |
| 403 with `Standard token exchange is not enabled for the requested client` (Keycloak) | The switch is off on OrionBelt's client. | Turn on **Standard token exchange** in its **Capability config**. |
| 403 with `unauthorized_client` | The identity provider did not accept OrionBelt's client. | `OIDC_CLIENT_ID` and the secret; on Okta, the **Token Exchange** grant of OrionBelt's app. |
| 403 naming a scope or audience | OrionBelt asked for a scope or audience its client may not have. | `DB_<NAME>_OAUTH_SCOPE` and `_OAUTH_AUDIENCE`, and the permission (Entra), access policy (Okta) or optional client scope (Keycloak) that grants them. |
| 401 `DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED` | The identity provider wants the user at its sign-in page first: multi-factor authentication, conditional access, or consent. | Sign the user in again. Over REST, `WWW-Authenticate` says where (`authorization_uri`) and carries Entra's claims challenge. Missing consent: grant admin consent to OrionBelt's permission. |
| Snowflake refuses the connection | The token reached Snowflake, and the integration or the user does not match it. | The integration is enabled; its issuer equals the token's `iss` exactly; its audience list contains the token's `aud`; its keys are the identity provider's; a user's login name equals the mapping claim; that user has the role the scope names, and the role is not blocked. Decode one exchanged token to compare. |

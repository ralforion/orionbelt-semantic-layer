# BigQuery

OrionBelt sends the user's access token to Google's security token service,
which exchanges it for a Google access token of the same user, a member of a
**workforce pool** (Google's workforce identity federation). BigQuery then runs
the query as that user, with the access Google Cloud IAM gives them.

Any OpenID Connect provider works, and OrionBelt needs no client secret.

## What you need

| What | Why |
|---|---|
| A Google Cloud organization | Workforce pools belong to an organization, and their users reach only projects inside it |
| BigQuery projects inside that organization | The projects queries run in and read from |
| Users signing in to OrionBelt with your identity provider | `AUTH_MODE=oidc`; see [OpenID Connect](../authentication.md#openid-connect) |
| `roles/iam.workforcePoolAdmin` on the organization | To create the pool and its provider |
| Permission to grant roles on the projects | To give the pool's users their access |

## 1. Create the workforce pool and its provider

```bash
gcloud iam workforce-pools create obsl \
  --organization=<organization ID> --location=global \
  --session-duration=3600s

gcloud iam workforce-pools providers create-oidc idp \
  --workforce-pool=obsl --location=global \
  --issuer-uri="<OIDC_ISSUER>" \
  --client-id="<OIDC_AUDIENCE>" \
  --attribute-mapping="google.subject=assertion.email" \
  --web-sso-response-type=id-token \
  --web-sso-assertion-claims-behavior=only-id-token-claims
```

| Setting | What to enter |
|---|---|
| `--issuer-uri` | Your identity provider's issuer, exactly as the tokens' `iss` claim has it: `OIDC_ISSUER` |
| `--client-id` | The audience of the tokens users send OrionBelt: `OIDC_AUDIENCE`. Google refuses a token whose `aud` differs. |
| `--attribute-mapping` | Which claim names the user in Google Cloud. `google.subject=assertion.email` makes their email their identity in the pool. |
| `--web-sso-*` | Needed to create the provider; OrionBelt does not use them (it calls the token service directly). |
| `--jwk-json-path` (optional) | A file with the identity provider's signing keys (JWKS), when Google cannot reach its issuer to read them |
| `--session-duration` | How long the Google tokens last: 900 s to 43200 s, default 3600 s |

The provider's full name is what OrionBelt needs:
`//iam.googleapis.com/locations/global/workforcePools/obsl/providers/idp`.

## 2. Give the pool's users their access

Every user needs to be allowed to use the project the exchange is billed to,
and to run BigQuery jobs. As a group, for all of the pool's users:

```bash
POOL='principalSet://iam.googleapis.com/locations/global/workforcePools/obsl/*'
gcloud projects add-iam-policy-binding <project> \
  --role=roles/serviceusage.serviceUsageConsumer --member="$POOL"
gcloud projects add-iam-policy-binding <project> \
  --role=roles/bigquery.jobUser --member="$POOL"
```

Which data each user may read is granted on the datasets or tables, as for
any other Google Cloud identity, for example `roles/bigquery.dataViewer` to
one user, `principal://iam.googleapis.com/locations/global/workforcePools/obsl/subject/jane@example.com`,
or to a group of them. That is where the per-user access lives.

## 3. Configure the data source

```bash
QUERY_EXECUTE=true
AUTH_MODE=oidc
OIDC_ISSUER=<your identity provider's issuer>
OIDC_AUDIENCE=<the provider's client ID>

OBSL_DATABASES=warehouse
DB_WAREHOUSE_TYPE=bigquery
DB_WAREHOUSE_AUTH=user
DB_WAREHOUSE_BIGQUERY_PROJECT=analytics-prod
DB_WAREHOUSE_WORKFORCE_PROVIDER=//iam.googleapis.com/locations/global/workforcePools/obsl/providers/idp
```

`DB_<NAME>_WORKFORCE_USER_PROJECT` sets the project billed for the exchange;
by default it is the source's `BIGQUERY_PROJECT`. A
`BIGQUERY_CREDENTIALS_FILE` set for the source is not used: each user's
queries run with their own Google token.

The provider and the billed project are part of the source's identity: two
sources that differ in either share no token and no cached result.

## 4. Check it

Run a query through OrionBelt as a signed-in user. BigQuery's job history
shows it under the user's workforce identity, and a user without access to a
table gets BigQuery's own permission error.

## Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| 403 `DATA_SOURCE_SIGN_IN_REFUSED`, the reason naming the audience | The token's `aud` is not the provider's client ID. | Set `--client-id` to `OIDC_AUDIENCE`. |
| 403 `DATA_SOURCE_SIGN_IN_REFUSED`, the reason naming the issuer or keys | Google could not check the token against the issuer. | `--issuer-uri` must equal the token's `iss`. If Google cannot reach the issuer, upload its keys with `--jwk-json-path`. |
| 403 with `Could not obtain a value for google.subject from the given credential` | The mapped claim is missing from the token. | Map a claim the token has, or add it at the identity provider. |
| BigQuery: `Caller does not have required permission to use project`, right after the grants | A new grant takes a minute or two to apply. | Wait, and run the query again. |
| BigQuery refuses the query | The user lacks a role, or the project is outside the organization. | `roles/bigquery.jobUser` and `roles/serviceusage.serviceUsageConsumer` on the project, data access on the dataset; the project inside the organization. |
| 503 `DATA_SOURCE_SIGN_IN_UNAVAILABLE` | OrionBelt could not reach `sts.googleapis.com`. | The network from OrionBelt to Google. |

Google limits token exchanges to 1,000 a minute per organization. OrionBelt
keeps each user's Google token until a minute before it expires, so a user
asking many questions exchanges once an hour.

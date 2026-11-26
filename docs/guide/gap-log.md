# Gap Log

When OrionBelt refuses a query, it keeps a short record of it. The model's author can then see what people and agents asked for that the model could not answer, and fix the model: add a synonym, a measure or a join.

The gap log is on by default. It keeps names and error codes, never values.

## What counts as a gap

A query is recorded when the model cannot answer it:

| Refusal | Example codes |
|---|---|
| A name the model does not have | `UNKNOWN_MEASURE`, `UNKNOWN_DIMENSION`, `UNKNOWN_SELECT_ITEM` (OBSQL) |
| A query OrionBelt refuses rather than return a wrong number | `FANOUT`, `GRAIN_NOT_SUBSET` |
| Something the database cannot do | `UNSUPPORTED_AGGREGATION`, `UNSUPPORTED_FUNCTION` |

Not recorded: a malformed request, an unknown model or dialect, warnings, and queries that return no rows.

## What is kept

| | `GAP_LOG=names` (default) | `GAP_LOG=full` |
|---|---|---|
| The error code | yes | yes |
| The name the error is about, and the names suggested instead | yes | yes |
| The dimensions, measures and fields the query asked for | yes | yes |
| Where the query came in: `rest` or `pgwire` | yes | yes |
| The error message | no | yes |
| The query's filters, with their values | no | yes |

Filter values can be customer data, such as an email address or a customer name. Use `full` only where keeping them is acceptable.

Never kept: SQL text, result rows, tokens or keys. Who asked is kept only as a short key that cannot be turned back into a name (see [Who sees what](#who-sees-what)).

Records are kept in memory: the newest `GAP_LOG_SIZE` records (default 10,000), across all models. A restart clears them. Each record is also written to the log as a `query_refused` event of the logger `orionbelt.gaps`, one JSON line with `LOG_FORMAT=json` or `cloudrun`, so your log store keeps the history.

## Reading the gaps

Ask for a model's gaps:

```bash
curl http://localhost:8000/v1/sessions/$SESSION/models/$MODEL/gaps
```

```json
{
  "model_id": "3f9a1c0b7e2d4a58",
  "mode": "names",
  "scope": "own",
  "gaps": [
    {
      "code": "UNKNOWN_MEASURE",
      "subject": "Net Rev",
      "count": 40,
      "callers": 1,
      "first_seen": "2026-11-26T08:02:11Z",
      "last_seen": "2026-11-26T09:12:40Z",
      "suggestions": ["Net Revenue"]
    }
  ],
  "recent": [
    {
      "at": "2026-11-26T09:12:40Z",
      "surface": "rest",
      "code": "UNKNOWN_MEASURE",
      "subject": "Net Rev",
      "suggestions": ["Net Revenue"],
      "dimensions": ["Customer Country"],
      "measures": ["Net Rev"],
      "fields": [],
      "message": null,
      "filters": null
    }
  ]
}
```

- `gaps` has one row per error code and name, the most frequent first.
- `recent` lists the latest refusals, newest first.
- `callers` says how many different callers asked. It is more than 1 only for an admin, who sees every caller's refusals.

In the example, agents asked for "Net Rev" 40 times and were pointed to `Net Revenue`. A query must use the model's own name, so add `Net Rev` to the `synonyms` of `Net Revenue`: `find` then returns it for that word, and an agent picks the right name before it queries.

Narrow the answer with query parameters:

| Parameter | Example | Effect |
|---|---|---|
| `code` | `?code=FANOUT` | Only this error code |
| `since` | `?since=2026-11-26T00:00:00Z` | Only refusals from this time on (UTC when no zone is given) |
| `limit` | `?limit=50` | How many recent refusals to list, 0 to 200 (default 20) |

With a single model loaded, `GET /v1/gaps` gives the same answer without the session and model ids.

## Who sees what

Each record carries a short key for who asked: twelve hex characters of a SHA-256 hash. You see only the records your own queries made.

| `AUTH_MODE` | You are known by | Everyone's gaps |
|---|---|---|
| `none` | The session in the request path | In the server log only |
| `api_key` | Your API key's id | In the server log only |
| `oidc` | Your user (the token's `sub`) | Signed-in users with the `obsl:admin` scope, who get `"scope": "all"` |

Without authentication, a request that names no session has no key:

| Request | Recorded? | Visible through the API? |
|---|---|---|
| A query to `/v1/sessions/{session_id}/...` | Yes | Yes, through the same session |
| A query to a shortcut, such as `/v1/query/sql` | Yes | No, in the server log only |
| A Postgres wire protocol query | Yes | No, in the server log only |

So without authentication, read the gaps through the session route. `GET /v1/gaps` lists nothing then, because it names no session.

All users of one MCP server that signs in with a single service token share that token's key. With per-user sign-in, each user has their own.

## Not recorded yet

Arrow Flight SQL queries are not recorded yet.

## Settings

| Variable | Default | Description |
|---|---|---|
| `GAP_LOG` | `names` | `names`, `full` or `off`. Any other value refuses the start |
| `GAP_LOG_SIZE` | `10000` | How many records are kept in memory, across all models. Must be at least 1 |

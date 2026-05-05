# Freshness contracts (the OBML `refresh:` block)

A freshness contract describes how the **physical table** that a `dataObject` maps to refreshes. It's the input to OBSL's :doc:`result cache <result-cache>` TTL composition. Authored once per source table.

## Where it lives

On the `dataObject`, alongside `code`, `database`, `schema`. Two `dataObject` entries that map to the same physical table should declare equivalent contracts; if they disagree, OBSL emits a `SHARED_TABLE_CONTRACT_DISAGREEMENT` warning at load time and applies the strictest.

```yaml
dataObjects:
  Orders:
    database: WAREHOUSE
    schema: PUBLIC
    code: ORDERS
    refresh:
      mode: interval
      interval: 1h
      anchor: "00:00"
      timezone: UTC
    columns:
      ...
```

## Modes

| Mode | When to use | Required fields |
|---|---|---|
| `interval` | Table refreshes on a fixed cadence (hourly batch, daily ETL, etc.) | `interval` |
| `heartbeat` | Table refreshes irregularly; an external job pings the heartbeat endpoint after each refresh | `max_staleness` |
| `static` | Table effectively never changes (lookup tables, country codes) | none |

A `dataObject` without a `refresh:` block is treated as **unknown**. By default, queries touching unknown-freshness tables are not cached (`CACHE_UNKNOWN_FRESHNESS_POLICY=no_cache`).

## Field reference

### Interval mode

| Field | Type | Default | Description |
|---|---|---|---|
| `interval` | duration | required | ISO 8601 (`PT1H`, `P1D`) or shorthand (`1h`, `15m`, `1d`). Sub-second values rejected. |
| `anchor` | `HH:MM` string | null | Optional time-of-day anchor. With no anchor, "next refresh" = `last_observed + interval`. With an anchor, refresh boundaries align to the anchor in `timezone`. |
| `timezone` | IANA TZ | `UTC` | Only used when `anchor` is set. |

### Heartbeat mode

| Field | Type | Default | Description |
|---|---|---|---|
| `max_staleness` (alias `maxStaleness`) | duration | required | Maximum time between heartbeats before the table is considered stale. The cache TTL is `max_staleness - time_since_last_heartbeat`. |

### Static mode

No fields. Use sparingly — appropriate for true reference data only. Static tables don't constrain TTL composition; a query whose every touched table is `static` caches up to `CACHE_MAX_TTL_SECONDS`.

## Multi-fact, one source

The cleanest case for source-level contracts:

```yaml
dataObjects:
  Sales:
    database: WAREHOUSE
    schema: PUBLIC
    code: ORDERS
    filter: "is_return = false"
    refresh:
      mode: heartbeat
      maxStaleness: 5m
  Returns:
    database: WAREHOUSE
    schema: PUBLIC
    code: ORDERS
    filter: "is_return = true"
    refresh:
      mode: heartbeat
      maxStaleness: 5m
```

`Sales` and `Returns` are two semantic facets on top of the same physical table. A multi-fact CFL query touching both collapses to **one** physical table reference (`WAREHOUSE.PUBLIC.ORDERS`). One heartbeat to that table invalidates every cached query depending on it — even if the query went through both `Sales` and `Returns`.

## OBSL graph integration

The contract is exposed in the model's RDF graph as `obsl:hasRefreshPolicy` on `obsl:DataObject`:

```sparql
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?label ?database ?schema ?code ?mode ?interval WHERE {
    ?do a obsl:DataObject ;
        rdfs:label ?label ;
        obsl:database ?database ;
        obsl:schema ?schema ;
        obsl:code ?code ;
        obsl:hasRefreshPolicy ?policy .
    ?policy obsl:refreshMode ?mode .
    OPTIONAL { ?policy obsl:refreshInterval ?interval }
}
```

Agents can ask the model what it expects of its sources — the freshness contract is data, not configuration.

## Validation

| Code | When |
|---|---|
| `REFRESH_PARSE_ERROR` | `refresh.mode` missing or invalid; `interval` mode missing `interval`; `heartbeat` mode missing `max_staleness`. |
| `SHARED_TABLE_CONTRACT_DISAGREEMENT` | Two dataObjects on the same physical table declare disagreeing refresh blocks. Warning, not error — the strictest contract is applied and the query keeps working. |

The strictness ordering: `unknown` > `heartbeat` > `interval` > `static`. Within the same mode, the smaller window wins (shorter interval, smaller `max_staleness`).

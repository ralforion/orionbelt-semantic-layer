# OBSL — RDF Graph & SPARQL

OBSL (OrionBelt Semantic Layer vocabulary) is an RDF-based exchange format for semantic-layer models. When you load a model, OrionBelt automatically exports it as an **OBSL-Core 0.1** RDF graph. You can retrieve the graph as Turtle or run read-only SPARQL queries against it — no extra setup required.

## What is OBSL-Core?

OBSL-Core 0.1 maps every OBML concept to RDF triples using standard vocabularies:

| OBML Concept | RDF Class | Key Properties |
|---|---|---|
| Model container | `obsl:SemanticModel` | `obsl:hasDataObject`, `obsl:hasDimension`, `obsl:hasMeasure`, `obsl:hasMetric` |
| Data Object | `obsl:DataObject` | `obsl:code`, `obsl:database`, `obsl:schema`, `obsl:hasColumn`, `obsl:hasJoin` |
| Column | `obsl:Column` | `obsl:code`, `obsl:resultType` |
| Join | `obsl:Join` | `obsl:joinTo`, `obsl:cardinality`, `obsl:columnFrom`, `obsl:columnTo` |
| Dimension | `obsl:Dimension` | `obsl:dataObject`, `obsl:column`, `obsl:resultType`, `obsl:timeGrain` |
| Measure | `obsl:Measure` | `obsl:aggregation`, `obsl:resultType`, `obsl:sourceColumn`, `obsl:expressionSource`, `obsl:filterExpression` |
| Metric | `obsl:Metric` | `obsl:metricType`, `obsl:expressionSource`, `obsl:baseMeasure`, `obsl:referencesMeasure` |
| Cumulative Metric | `obsl:CumulativeMetric` | `obsl:timeDimension`, `obsl:cumulativeType`, `obsl:window`, `obsl:grainToDate` |
| Period-over-Period Metric | `obsl:PeriodOverPeriodMetric` | `obsl:timeDimension`, `obsl:timeGrain`, `obsl:offset`, `obsl:offsetGrain`, `obsl:comparison` |

Labels use `rdfs:label`, synonyms use `obsl:synonym`, and descriptions use `rdfs:comment`.

!!! info "Namespace"
    ```
    @prefix obsl: <https://ralforion.com/ns/obsl#> .
    ```
    Vocabulary reference: [https://ralforion.com/ns/obsl/](https://ralforion.com/ns/obsl/)

## Retrieving the Graph

After loading a model, retrieve its RDF graph as Turtle:

=== "curl"

    ```bash
    curl http://localhost:8000/v1/sessions/{session_id}/models/{model_id}/graph
    ```

=== "Shortcut (single model)"

    ```bash
    curl http://localhost:8000/v1/graph
    ```

The response is `text/turtle`:

```turtle
@prefix obsl: <https://ralforion.com/ns/obsl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<https://ralforion.com/ns/model/abc123> a obsl:SemanticModel ;
    obsl:hasDataObject <.../data-object/orders> ;
    obsl:hasDimension <.../dimension/country> ;
    obsl:hasMeasure <.../measure/revenue> .

<.../measure/revenue> a obsl:Measure ;
    rdfs:label "Revenue" ;
    obsl:aggregation "sum" ;
    obsl:resultType "float" ;
    obsl:expressionSource "{[Orders].[Price]} * {[Orders].[Quantity]}" .
```

## SPARQL Queries

Run read-only SPARQL (`SELECT` and `ASK`) against any loaded model:

=== "curl"

    ```bash
    curl -X POST http://localhost:8000/v1/sessions/{session_id}/models/{model_id}/sparql \
      -H "Content-Type: application/json" \
      -d '{"query": "PREFIX obsl: <https://ralforion.com/ns/obsl#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> SELECT ?label WHERE { ?m a obsl:Measure ; rdfs:label ?label . }"}'
    ```

=== "Shortcut (single model)"

    ```bash
    curl -X POST http://localhost:8000/v1/sparql \
      -H "Content-Type: application/json" \
      -d '{"query": "PREFIX obsl: <https://ralforion.com/ns/obsl#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> SELECT ?label WHERE { ?m a obsl:Measure ; rdfs:label ?label . }"}'
    ```

### SELECT example

List all measures with their aggregation:

```sparql
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?label ?agg WHERE {
    ?m a obsl:Measure ;
       rdfs:label ?label ;
       obsl:aggregation ?agg .
}
```

Response:

```json
{
  "type": "select",
  "variables": ["label", "agg"],
  "results": [
    {"label": "Revenue", "agg": "sum"},
    {"label": "Order Count", "agg": "count"}
  ],
  "boolean": null
}
```

### ASK example

Check if any dimension exists:

```sparql
PREFIX obsl: <https://ralforion.com/ns/obsl#>
ASK { ?x a obsl:Dimension }
```

Response:

```json
{
  "type": "ask",
  "variables": [],
  "results": [],
  "boolean": true
}
```

### More query ideas

```sparql
-- Find all joins and their cardinality
PREFIX obsl: <https://ralforion.com/ns/obsl#>
SELECT ?from ?to ?card WHERE {
    ?j a obsl:Join ;
       obsl:joinTo ?to ;
       obsl:cardinality ?card .
    ?from obsl:hasJoin ?j .
}
```

```sparql
-- Find all synonyms across the model
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?label ?synonym WHERE {
    ?x rdfs:label ?label ;
       obsl:synonym ?synonym .
}
```

```sparql
-- Find metrics that reference a specific measure
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?metric ?measure WHERE {
    ?m a obsl:Metric ;
       rdfs:label ?metric ;
       obsl:referencesMeasure ?ref .
    ?ref rdfs:label ?measure .
}
```

!!! warning "Read-only"
    Only `SELECT` and `ASK` queries are allowed. Update operations (`INSERT`, `DELETE`, `LOAD`, `DROP`, etc.) return HTTP 400.

## How It Works

The OBSL graph is generated **eagerly at model load time** — there is no extra step to trigger it. When you call `POST /v1/sessions/{id}/models` to load a model, the graph is built and cached alongside the `SemanticModel`. Subsequent `/graph` and `/sparql` calls read from this cache, so they are fast.

The graph is removed when the model is unloaded (`DELETE /v1/sessions/{id}/models/{mid}`).

## Specification

The full OBSL-Core 0.1 specification — including all classes, properties, URI strategy, OBML mapping, and controlled value sets — is in [`ontology/spec.md`](https://github.com/ralfbecher/orionbelt-semantic-layer/blob/main/ontology/spec.md).

The OWL ontology (`obsl.ttl`), SHACL shapes (`obsl.shacl.ttl`), and a Sales model example (`example-sales.ttl`) are available in the [`ontology/`](https://github.com/ralfbecher/orionbelt-semantic-layer/tree/main/ontology) directory and at [https://ralforion.com/ns/obsl/](https://ralforion.com/ns/obsl/).

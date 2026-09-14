---
description: "Every loaded model is also emitted as an RDF graph in the OBSL vocabulary and queryable with SPARQL, for semantic-layer interoperability, governance and knowledge exchange."
---

# OBSL — RDF Graph & SPARQL

OBSL (OrionBelt Semantic Layer vocabulary) is an RDF-based exchange format for semantic-layer models. When you load a model, OrionBelt automatically exports it as an **OBSL-Core 0.2** RDF graph. You can retrieve the graph as Turtle or run read-only SPARQL queries against it — no extra setup required.

## What is OBSL-Core?

OBSL-Core 0.2 maps every OBML concept to RDF triples using standard vocabularies:

| OBML Concept | RDF Class | Key Properties |
|---|---|---|
| Model container | `obsl:SemanticModel` | `obsl:hasDataObject`, `obsl:hasDimension`, `obsl:hasMeasure`, `obsl:hasMetric` |
| Data Object | `obsl:DataObject` | `obsl:code`, `obsl:database`, `obsl:schema`, `obsl:hasColumn`, `obsl:hasJoin` |
| Column | `obsl:Column` | `obsl:code`, `obsl:resultType` |
| Join | `obsl:Join` | `obsl:joinTo`, `obsl:cardinality`, `obsl:columnFrom`, `obsl:columnTo` |
| Dimension | `obsl:Dimension` | `obsl:dataObject`, `obsl:column`, `obsl:resultType`, `obsl:timeGrain` |
| Measure | `obsl:Measure` | `obsl:aggregation`, `obsl:resultType`, `obsl:sourceColumn`, `obsl:expressionSource`, `obsl:filterExpression`, `obsl:grainMode`, `obsl:grainInclude`, `obsl:filterContextMode`, `obsl:owner`, `obsl:dataType`, `obsl:format` |
| Metric | `obsl:Metric` | `obsl:metricType`, `obsl:expressionSource`, `obsl:baseMeasure`, `obsl:referencesMeasure`, `obsl:owner`, `obsl:dataType`, `obsl:format` |
| Cumulative Metric | `obsl:CumulativeMetric` | `obsl:timeDimension`, `obsl:cumulativeType`, `obsl:window`, `obsl:grainToDate` |
| Period-over-Period Metric | `obsl:PeriodOverPeriodMetric` | `obsl:timeDimension`, `obsl:timeGrain`, `obsl:offset`, `obsl:offsetGrain`, `obsl:comparison` |
| External concept mapping | `obsl:ExternalConceptMapping` (plus a direct `skos:*Match` triple on the artefact) | `obsl:sourceObject`, `obsl:targetConcept`, `obsl:authoredConcept`, `obsl:mappingRelation`, `obsl:mappingJustification`, `obsl:mappingSource`, `obsl:ontologyVersion`, `obsl:confidence`, `obsl:mappingComment` |

Labels use `rdfs:label`, synonyms use `obsl:synonym`, and descriptions use `rdfs:comment`.

### External concept mappings in the graph

An [`externalConceptMappings`](concept-mappings.md) entry becomes a direct SKOS mapping triple from the artefact to the expanded external IRI, so any SKOS-aware consumer reads it without knowing OBSL:

```turtle
<https://ralforion.com/ns/model/sales/measure/revenue>
    skos:exactMatch <https://ontology.example.com/business/NetRevenue> .
```

`exact` is `skos:exactMatch`, `close` is `skos:closeMatch`, `broader` is `skos:broadMatch`, `narrower` is `skos:narrowMatch`, `related` is `skos:relatedMatch`. A mapping that carries provenance (justification, source, ontology version, confidence, comment) additionally gets an `obsl:ExternalConceptMapping` resource holding it, at a deterministic IRI under the artefact. The model's `ontology.prefixes` and `skos` are bound on the graph, so a serialized Turtle shows `corp:NetRevenue` the way the author wrote it. The external ontology itself is never imported: the graph references its IRIs and says nothing about them.

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

The [Gradio UI](ui.md) has a SPARQL tab with a gallery of the examples below, run against whatever model is loaded in the editor.

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
# Find all joins and their cardinality
PREFIX obsl: <https://ralforion.com/ns/obsl#>
SELECT ?from ?to ?card WHERE {
    ?j a obsl:Join ;
       obsl:joinTo ?to ;
       obsl:cardinality ?card .
    ?from obsl:hasJoin ?j .
}
```

```sparql
# Find all synonyms across the model
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?label ?synonym WHERE {
    ?x rdfs:label ?label ;
       obsl:synonym ?synonym .
}
```

```sparql
# Find metrics that reference a specific measure
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?metric ?measure WHERE {
    ?m a obsl:Metric ;
       rdfs:label ?metric ;
       obsl:referencesMeasure ?ref .
    ?ref rdfs:label ?measure .
}
```

```sparql
# Every modeled artefact by type and label
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?type ?label WHERE {
    ?x a ?type ;
       rdfs:label ?label .
    FILTER(?type IN (obsl:DataObject, obsl:Dimension, obsl:Measure, obsl:Metric))
}
ORDER BY ?type ?label
```

```sparql
# Measures and the physical columns they aggregate
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?measure ?aggregation ?column WHERE {
    ?m a obsl:Measure ;
       rdfs:label ?measure ;
       obsl:aggregation ?aggregation ;
       obsl:sourceColumn ?c .
    ?c obsl:code ?column .
}
```

```sparql
# Which artefacts link into one external namespace, and with what provenance?
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?label ?relation ?concept ?justification ?source WHERE {
    ?x rdfs:label ?label ;
       ?relation ?concept .
    FILTER(?relation IN (skos:exactMatch, skos:closeMatch, skos:broadMatch,
                         skos:narrowMatch, skos:relatedMatch))
    FILTER(STRSTARTS(STR(?concept), "https://example.com/ontology/commerce/"))
    OPTIONAL {
        ?x obsl:hasExternalConceptMapping ?mapping .
        ?mapping obsl:targetConcept ?concept ;
                 obsl:mappingJustification ?justification ;
                 obsl:mappingSource ?source .
    }
}
ORDER BY ?label
```

```sparql
# What does each measure mean in the corporate ontology?
# externalConceptMappings become direct skos:*Match triples from the
# element to the external IRI; one with provenance also has an
# obsl:ExternalConceptMapping resource (source, justification, confidence).
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?measure ?relation ?concept WHERE {
    ?m a obsl:Measure ;
       rdfs:label ?measure ;
       ?relation ?concept .
    FILTER(?relation IN (skos:exactMatch, skos:closeMatch, skos:broadMatch,
                         skos:narrowMatch, skos:relatedMatch))
}
```

!!! tip "Unbound variables are warned about"
    `ORDER BY ?lable` for a query that binds `?label` is valid SPARQL: an unbound variable compares equal everywhere, so nothing is ordered and no engine complains. Since that is nearly always a typo, the endpoint (and the UI) returns a `warnings` entry for a variable that is ordered by or projected but never bound in a triple pattern, `BIND` or `VALUES`.

!!! warning "Read-only"
    Only `SELECT` and `ASK` queries are allowed. Update operations (`INSERT`, `DELETE`, `LOAD`, `DROP`, etc.) return HTTP 400.

## How It Works

The OBSL graph is generated **eagerly at model load time** — there is no extra step to trigger it. When you call `POST /v1/sessions/{id}/models` to load a model, the graph is built and cached alongside the `SemanticModel`. Subsequent `/graph` and `/sparql` calls read from this cache, so they are fast.

The graph is removed when the model is unloaded (`DELETE /v1/sessions/{id}/models/{mid}`).

## Specification

The full OBSL-Core 0.2 specification — including all classes, properties, URI strategy, OBML mapping, and controlled value sets — is in [`ontology/spec.md`](https://github.com/ralforion/orionbelt-semantic-layer/blob/main/ontology/spec.md).

The OWL ontology (`obsl.ttl`), SHACL shapes (`obsl.shacl.ttl`), and a Sales model example (`example-sales.ttl`) are available in the [`ontology/`](https://github.com/ralforion/orionbelt-semantic-layer/tree/main/ontology) directory and at [https://ralforion.com/ns/obsl/](https://ralforion.com/ns/obsl/).

The same files are published at [https://ralforion.com/ns/obsl/obsl.ttl](https://ralforion.com/ns/obsl/obsl.ttl), [obsl.shacl.ttl](https://ralforion.com/ns/obsl/obsl.shacl.ttl) and [example-sales.ttl](https://ralforion.com/ns/obsl/example-sales.ttl), next to the [vocabulary page](https://ralforion.com/ns/obsl/); the docs deploy copies them from `ontology/` on every release, so the published copy is the repository's. The version IRI `https://ralforion.com/ns/obsl/0.2` resolves to a version page with the same downloads.

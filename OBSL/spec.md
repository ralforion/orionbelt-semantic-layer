# OBSL-Core 0.1

Status: Finalized core profile

## 1. Abstract
OBSL, the OrionBelt Semantic Layer vocabulary, is an RDF-based exchange format for semantic-layer models. `OBSL-Core 0.1` is the finalized minimal exchange profile. It represents data objects, columns, joins, dimensions, measures, and metrics in a graph form suitable for interoperability, governance, and knowledge exchange.

OBSL uses:
- RDF for machine-readable structure
- RDFS for labels and descriptions
- SHACL for optional validation
- a custom `obsl:` namespace for semantic-layer meaning

## 2. Goals
OBSL-Core is designed to:
- exchange semantic models between systems
- preserve logical semantics rather than SQL compilation details
- provide a stable core profile before richer extensions
- align with existing OBML concepts
- enable graph-native querying and governance use cases

OBSL-Core is not intended to represent:
- full query plans
- dialect-specific SQL
- runtime planner internals
- structured expression ASTs
- explicit filter graphs

## 3. Profiles

### 3.1 OBSL-Core 0.1
`OBSL-Core 0.1` includes:
- semantic model container
- data objects
- columns
- joins
- dimensions
- measures
- metrics
- expression strings
- labels, descriptions, and synonyms

### 3.2 OBSL-Full
OBSL-Full adds:
- structured expression graphs
- explicit filter graphs
- cumulative metric metadata
- period-over-period metadata
- SHACL validation

Only `OBSL-Core 0.1` is finalized by this document. `OBSL-Full` remains future work.

## 4. Namespaces
Final prefixes:

```ttl
@prefix rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .
@prefix sh:   <http://www.w3.org/ns/shacl#> .
@prefix obsl: <https://ralforion.com/ns/obsl#> .
```

The `obsl:` namespace is frozen for `OBSL-Core 0.1` as:
- `https://ralforion.com/ns/obsl#`
- Hosted at: [https://ralforion.com/ns/obsl/](https://ralforion.com/ns/obsl/)

## 5. Core Model

### 5.1 SemanticModel
Top-level container for a semantic model.

Required:
- `rdf:type obsl:SemanticModel`

Optional:
- `rdfs:label`
- `rdfs:comment`
- `obsl:hasDataObject`
- `obsl:hasDimension`
- `obsl:hasMeasure`
- `obsl:hasMetric`

Cardinality:
- any number of contained resources
- the containment properties are optional individually, since a valid model may omit a category

### 5.2 DataObject
Represents a logical or physical source relation.

Required:
- `rdfs:label`
- `obsl:code`
- `obsl:database`
- `obsl:schema`

Optional:
- `obsl:physicalName`
- `obsl:hasColumn`
- `obsl:hasJoin`
- `obsl:synonym`
- `rdfs:comment`

Cardinality:
- exactly one `rdfs:label`
- exactly one `obsl:code`
- exactly one `obsl:database`
- exactly one `obsl:schema`

### 5.3 Column
Represents a logical column within a data object.

Required:
- `rdfs:label`
- `obsl:code`
- `obsl:resultType`

Optional:
- `obsl:belongsToDataObject`
- `obsl:synonym`
- `rdfs:comment`

Cardinality:
- exactly one `rdfs:label`
- exactly one `obsl:code`
- exactly one `obsl:resultType`

### 5.4 Join
Represents a semantic join edge between data objects.

Required:
- `obsl:joinTo`
- `obsl:cardinality`
- `obsl:columnFrom`
- `obsl:columnTo`

Optional:
- `obsl:secondary`
- `obsl:pathName`

Cardinality:
- exactly one `obsl:joinTo`
- exactly one `obsl:cardinality`
- one or more `obsl:columnFrom`
- one or more `obsl:columnTo`
- if `obsl:secondary` is `true`, `obsl:pathName` SHOULD be present

### 5.5 Dimension
Represents an analytic dimension.

Required:
- `rdfs:label`
- `obsl:dataObject`
- `obsl:column`
- `obsl:resultType`

Optional:
- `obsl:timeGrain`
- `obsl:synonym`
- `rdfs:comment`

Cardinality:
- exactly one `rdfs:label`
- exactly one `obsl:dataObject`
- exactly one `obsl:column`
- exactly one `obsl:resultType`
- at most one `obsl:timeGrain`

### 5.6 Measure
Represents an aggregated analytic field.

Required:
- `rdfs:label`
- `obsl:aggregation`
- `obsl:resultType`

A measure MUST define exactly one source form:
- one or more `obsl:sourceColumn`
- `obsl:expressionSource`

Optional:
- `obsl:distinct`
- `obsl:total`
- `obsl:allowFanOut`

Cardinality:
- exactly one `rdfs:label`
- exactly one `obsl:aggregation`
- exactly one `obsl:resultType`
- either one or more `obsl:sourceColumn`, or exactly one `obsl:expressionSource`

Out of scope for Core:
- `obsl:hasFilter`
- `obsl:hasExpression`

### 5.7 Metric
Represents a derived, cumulative, or period-over-period metric at core metadata level.

Required:
- `rdfs:label`
- `obsl:metricType`

A metric MUST define at least one semantic source:
- `obsl:expressionSource`
- `obsl:baseMeasure`

Optional:
- `obsl:referencesMeasure`
Cardinality:
- exactly one `rdfs:label`
- exactly one `obsl:metricType`
- at least one of `obsl:expressionSource` or `obsl:baseMeasure`

Optional Core properties:
- `obsl:referencesMeasure`

Out of scope for Core:
- `obsl:hasExpression`
- `obsl:timeDimension`
- `obsl:cumulativeType`
- `obsl:window`
- `obsl:grainToDate`
- `obsl:offset`
- `obsl:offsetGrain`
- `obsl:comparison`

## 6. Expressions
In `OBSL-Core 0.1`, the normative expression representation is:
- `obsl:expressionSource`

Structured expression graphs are explicitly out of scope for `OBSL-Core 0.1`.

Core-compatible derived links:
- metrics MAY include `obsl:referencesMeasure`
- expression-based measures MAY include `obsl:referencesColumn` in a future compatible extension, but this is not required for Core conformance

## 7. Filters
Explicit filter graphs are out of scope for `OBSL-Core 0.1`.

If a system needs filter exchange, it should use a future `OBSL-Full` profile.

## 8. Controlled Value Sets

### 8.1 Result Types
- `string`
- `json`
- `int`
- `float`
- `date`
- `time`
- `time_tz`
- `timestamp`
- `timestamp_tz`
- `boolean`

### 8.2 Aggregations
- `sum`
- `count`
- `count_distinct`
- `avg`
- `min`
- `max`
- `any_value`
- `median`
- `mode`
- `listagg`

### 8.3 Join Cardinalities
- `many-to-one`
- `one-to-one`
- `many-to-many`

### 8.4 Metric Types
- `derived`
- `cumulative`
- `period_over_period`

### 8.5 Time Grains
- `year`
- `quarter`
- `month`
- `week`
- `day`
- `hour`
- `minute`
- `second`

### 8.6 Period Comparison Types
- `ratio`
- `difference`
- `previousValue`
- `percentChange`

## 9. URI Strategy
Final URI guidance:

- `/model/{model-id}`
- `/model/{model-id}/data-object/{name}`
- `/model/{model-id}/data-object/{object}/column/{name}`
- `/model/{model-id}/dimension/{name}`
- `/model/{model-id}/measure/{name}`
- `/model/{model-id}/metric/{name}`
- `/model/{model-id}/expr/{id}`

URIs SHOULD be stable. Labels SHOULD NOT be treated as immutable identifiers.

Recommended practice:
- derive resource URIs from stable slugs, not transient display strings
- use one URI space per model
- keep resource identity independent from serialization order

## 10. OBML Mapping

### 10.1 Top Level
- `dataObjects` -> `obsl:hasDataObject`
- `dimensions` -> `obsl:hasDimension`
- `measures` -> `obsl:hasMeasure`
- `metrics` -> `obsl:hasMetric`
- `description` -> `rdfs:comment`

### 10.2 Data Objects
- `code` -> `obsl:code`
- `database` -> `obsl:database`
- `schema` -> `obsl:schema`
- `synonyms[]` -> `obsl:synonym`

### 10.3 Columns
- `code` -> `obsl:code`
- `abstractType` -> `obsl:resultType`

### 10.4 Joins
- `joinType` -> `obsl:cardinality`
- `joinTo` -> `obsl:joinTo`
- `columnsFrom[]` -> `obsl:columnFrom`
- `columnsTo[]` -> `obsl:columnTo`
- `secondary` -> `obsl:secondary`
- `pathName` -> `obsl:pathName`

### 10.5 Dimensions
- `dataObject` -> `obsl:dataObject`
- `column` -> `obsl:column`
- `resultType` -> `obsl:resultType`
- `timeGrain` -> `obsl:timeGrain`

### 10.6 Measures
- `columns[]` -> `obsl:sourceColumn`
- `aggregation` -> `obsl:aggregation`
- `resultType` -> `obsl:resultType`
- `expression` -> `obsl:expressionSource`
- `distinct` -> `obsl:distinct`
- `total` -> `obsl:total`
- `allowFanOut` -> `obsl:allowFanOut`

### 10.7 Metrics
- `type` -> `obsl:metricType`
- `expression` -> `obsl:expressionSource`
- `measure` -> `obsl:baseMeasure`
- derived measure references parsed from `expression` -> `obsl:referencesMeasure`

Fields intentionally excluded from Core mapping:
- measure filters
- expression AST nodes
- cumulative metric details
- period-over-period detail fields

## 11. Validation
SHACL MAY be used to enforce Core rules such as:
- required properties
- mutually exclusive source forms
- join completeness
- model-level structural consistency

## 12. Versioning
OBSL-Core 0.1 keeps:
- expression strings as normative
- AST out of scope
- planner details out of scope
- vocabulary small and close to OBML

## 13. Finalized Core Surface
The finalized `OBSL-Core 0.1` surface is:

Classes:
- `obsl:SemanticModel`
- `obsl:DataObject`
- `obsl:Column`
- `obsl:Join`
- `obsl:Dimension`
- `obsl:Measure`
- `obsl:Metric`

Core object properties:
- `obsl:hasDataObject`
- `obsl:hasDimension`
- `obsl:hasMeasure`
- `obsl:hasMetric`
- `obsl:hasColumn`
- `obsl:hasJoin`
- `obsl:joinTo`
- `obsl:columnFrom`
- `obsl:columnTo`
- `obsl:dataObject`
- `obsl:column`
- `obsl:sourceColumn`
- `obsl:baseMeasure`
- `obsl:referencesMeasure`

Core datatype properties:
- `obsl:code`
- `obsl:database`
- `obsl:schema`
- `obsl:physicalName`
- `obsl:resultType`
- `obsl:aggregation`
- `obsl:metricType`
- `obsl:cardinality`
- `obsl:timeGrain`
- `obsl:expressionSource`
- `obsl:synonym`
- `obsl:secondary`
- `obsl:pathName`
- `obsl:distinct`
- `obsl:total`
- `obsl:allowFanOut`

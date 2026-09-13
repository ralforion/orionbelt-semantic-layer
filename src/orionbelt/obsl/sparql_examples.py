"""Ready-to-run SPARQL examples over the exported OBSL graph.

One gallery, used by the Gradio UI's SPARQL tab and checked by tests
against the commerce demo's graph, so every entry is known to parse and
to find something in the model the public playground loads. Each query
carries its own PREFIX lines: the SPARQL endpoint binds none.
"""

from __future__ import annotations

from dataclasses import dataclass

_PREFIXES = """\
PREFIX obsl: <https://ralforion.com/ns/obsl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
"""


@dataclass(frozen=True)
class SparqlExample:
    """A titled, self-contained SPARQL query."""

    title: str
    query: str


SPARQL_EXAMPLES: tuple[SparqlExample, ...] = (
    SparqlExample(
        "Artefacts by type and label",
        _PREFIXES
        + """
SELECT ?type ?label WHERE {
    ?x a ?type ;
       rdfs:label ?label .
    FILTER(?type IN (obsl:DataObject, obsl:Dimension, obsl:Measure, obsl:Metric))
}
ORDER BY ?type ?label
""",
    ),
    SparqlExample(
        "Measures and the columns they aggregate",
        _PREFIXES
        + """
SELECT ?measure ?aggregation ?column WHERE {
    ?m a obsl:Measure ;
       rdfs:label ?measure ;
       obsl:aggregation ?aggregation ;
       obsl:sourceColumn ?c .
    ?c obsl:code ?column .
}
ORDER BY ?measure
""",
    ),
    SparqlExample(
        "Metrics and the measures they reference",
        _PREFIXES
        + """
SELECT ?metric ?measure WHERE {
    ?m a obsl:Metric ;
       rdfs:label ?metric ;
       obsl:referencesMeasure ?ref .
    ?ref rdfs:label ?measure .
}
ORDER BY ?metric ?measure
""",
    ),
    SparqlExample(
        "Joins between data objects",
        _PREFIXES
        + """
SELECT ?from ?to ?cardinality WHERE {
    ?f obsl:hasJoin ?j ;
       rdfs:label ?from .
    ?j obsl:joinTo ?t ;
       obsl:cardinality ?cardinality .
    ?t rdfs:label ?to .
}
ORDER BY ?from ?to
""",
    ),
    SparqlExample(
        "External concept mappings",
        _PREFIXES
        + """
SELECT ?label ?relation ?concept WHERE {
    ?x rdfs:label ?label ;
       ?relation ?concept .
    FILTER(?relation IN (skos:exactMatch, skos:closeMatch, skos:broadMatch,
                         skos:narrowMatch, skos:relatedMatch))
}
ORDER BY ?label ?relation
""",
    ),
    SparqlExample(
        "Mappings with provenance",
        _PREFIXES
        + """
SELECT ?label ?concept ?justification ?source ?version ?confidence WHERE {
    ?x rdfs:label ?label ;
       obsl:hasExternalConceptMapping ?mapping .
    ?mapping obsl:targetConcept ?concept .
    OPTIONAL { ?mapping obsl:mappingJustification ?justification }
    OPTIONAL { ?mapping obsl:mappingSource ?source }
    OPTIONAL { ?mapping obsl:ontologyVersion ?version }
    OPTIONAL { ?mapping obsl:confidence ?confidence }
}
ORDER BY ?label
""",
    ),
    SparqlExample(
        "Does the model link into schema.org? (ASK)",
        _PREFIXES
        + """
ASK {
    ?x ?relation ?concept .
    FILTER(?relation IN (skos:exactMatch, skos:closeMatch, skos:broadMatch,
                         skos:narrowMatch, skos:relatedMatch))
    FILTER(STRSTARTS(STR(?concept), "https://schema.org/"))
}
""",
    ),
)

EXAMPLE_TITLES: tuple[str, ...] = tuple(example.title for example in SPARQL_EXAMPLES)


def example_query(title: str) -> str:
    """The query behind a gallery title, or an empty string for an unknown one."""
    for example in SPARQL_EXAMPLES:
        if example.title == title:
            return example.query
    return ""

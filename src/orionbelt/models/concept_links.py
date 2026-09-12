"""External concept links: prefix handling and IRI expansion.

``externalConceptMappings`` name a concept either as a compact IRI
(``corp:NetRevenue``) that the model's ``ontology.prefixes`` expand, or as a
full IRI. This module owns the rules both forms follow, so the resolver, the
RDF exporter and the discovery index agree on what a concept expands to.

Nothing here dereferences a remote IRI: the checks are syntactic.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

# Prefixes every model may use without declaring them. An authored
# ``ontology.prefixes`` entry may repeat one of these verbatim but may not
# bind the name to a different namespace.
BUILTIN_PREFIXES: Mapping[str, str] = MappingProxyType(
    {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "owl": "http://www.w3.org/2002/07/owl#",
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
    }
)

# Conservative prefix names: an XML NCName-like identifier. Deliberately
# narrower than Turtle's PN_PREFIX so a prefix never needs escaping in any
# serialization the graph may be written to.
PREFIX_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):(.*)$", re.DOTALL)
# Characters RFC 3987 excludes from an IRI (and whitespace).
_FORBIDDEN_IRI_CHARS = frozenset(' \t\r\n<>"{}|\\^`')


class ConceptIriError(ValueError):
    """An authored concept or namespace value that cannot become an absolute IRI.

    ``code`` is the OBML error code the resolver reports
    (``INVALID_CONCEPT_IRI`` or ``UNKNOWN_ONTOLOGY_PREFIX``).
    """

    def __init__(self, code: str, message: str, suggestions: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestions = suggestions or []


def is_absolute_iri(value: object) -> bool:
    """Whether *value* is a full IRI rather than a compact one or a fragment.

    A full IRI has a scheme followed by an authority (``https://…``) or is a
    URN (``urn:…``), and contains none of the characters RFC 3987 excludes.
    ``corp:NetRevenue`` has a scheme-shaped prefix too, so a bare
    ``scheme:`` is not enough to count as absolute: that is exactly the
    shape of a compact IRI, and treating it as absolute would let an
    undeclared prefix through silently.
    """
    if not isinstance(value, str) or not value:
        return False
    if _FORBIDDEN_IRI_CHARS.intersection(value):
        return False
    m = _SCHEME_RE.match(value)
    if m is None:
        return False
    scheme, rest = m.group(1).lower(), m.group(2)
    if scheme == "urn":
        return bool(rest)
    return rest.startswith("//") and len(rest) > 2


def effective_prefixes(declared: Mapping[str, str] | None) -> dict[str, str]:
    """The built-in prefixes plus the model's declared ones."""
    merged = dict(BUILTIN_PREFIXES)
    merged.update(declared or {})
    return merged


def expand_concept(concept: object, prefixes: Mapping[str, str]) -> str:
    """Return the absolute IRI an authored ``concept`` value denotes.

    A full IRI is returned as written. A compact IRI ``prefix:local`` is
    expanded with *prefixes* (use :func:`effective_prefixes` to include the
    built-ins). Blank nodes, relative references, angle-bracketed IRIs and
    values with illegal characters raise :class:`ConceptIriError`.
    """
    if not isinstance(concept, str) or not concept:
        raise ConceptIriError("INVALID_CONCEPT_IRI", "'concept' must be a non-empty string")
    if concept.startswith("_:"):
        raise ConceptIriError(
            "INVALID_CONCEPT_IRI",
            f"'{concept}' is a blank node identifier; a concept must be an IRI",
        )
    if concept.startswith("<") and concept.endswith(">"):
        raise ConceptIriError(
            "INVALID_CONCEPT_IRI",
            f"'{concept}' is wrapped in angle brackets; write the IRI without them",
        )
    if is_absolute_iri(concept):
        return concept
    bad = sorted(_FORBIDDEN_IRI_CHARS.intersection(concept))
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        raise ConceptIriError(
            "INVALID_CONCEPT_IRI", f"'{concept}' contains characters not allowed in an IRI: {shown}"
        )
    prefix, sep, local = concept.partition(":")
    if not sep:
        raise ConceptIriError(
            "INVALID_CONCEPT_IRI",
            f"'{concept}' is a relative reference; use a full IRI or a declared prefix "
            "(e.g. 'corp:NetRevenue')",
        )
    if not local:
        raise ConceptIriError(
            "INVALID_CONCEPT_IRI", f"'{concept}' has no local name after the prefix"
        )
    namespace = prefixes.get(prefix)
    if namespace is None:
        raise ConceptIriError(
            "UNKNOWN_ONTOLOGY_PREFIX",
            f"Unknown prefix '{prefix}' in '{concept}'; declare it under ontology.prefixes",
            suggestions=sorted(prefixes),
        )
    return namespace + local

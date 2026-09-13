"""Model-local index of external concept mappings.

Answers the discovery questions the REST API and, later, context packages
ask: which objects map to a given concept, which namespaces a model links
into, and which objects in the mappable scope carry no mapping yet. Built
from a resolved :class:`SemanticModel` on demand; it is a handful of dict
lookups over a few hundred entries at most, so nothing is cached at load.

Object references use the OBML vocabulary (``model``, ``dataObject``,
``dimension``, ``measure``, ``metric``, ``rule``). Synthesized count measures are not
in scope: authors cannot attach mappings to them, so listing them as
unmapped would only be noise.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from orionbelt.models.concept_links import (
    ConceptIriError,
    effective_prefixes,
    expand_concept,
    is_absolute_iri,
)
from orionbelt.models.semantic import ExternalConceptMapping, SemanticModel

MAPPABLE_TYPES: tuple[str, ...] = ("model", "dataObject", "dimension", "measure", "metric", "rule")


@dataclass(frozen=True)
class SemanticObjectRef:
    """A model artefact by kind and name."""

    type: str
    name: str


@dataclass(frozen=True)
class ConceptLink:
    """One mapping, with the object it sits on and its absolute target IRI."""

    object: SemanticObjectRef
    mapping: ExternalConceptMapping
    expanded_iri: str


@dataclass(frozen=True)
class NamespaceUsage:
    """How much of a model links into one external namespace."""

    prefix: str | None
    namespace: str
    mapping_count: int
    object_count: int


def _split_namespace(iri: str) -> str:
    """The namespace part of an IRI when no declared prefix covers it."""
    for sep in ("#", "/"):
        head, found, _ = iri.rpartition(sep)
        if found:
            return head + sep
    return iri


class ConceptMappingIndex:
    """Lookups over every external concept mapping in one model."""

    def __init__(self, model: SemanticModel, model_name: str) -> None:
        self.prefixes = effective_prefixes(model.ontology.prefixes if model.ontology else None)
        self.declared_prefixes = dict(model.ontology.prefixes) if model.ontology else {}
        self._links: list[ConceptLink] = []
        self._objects: list[SemanticObjectRef] = []
        for ref, mappings in self._iter_objects(model, model_name):
            self._objects.append(ref)
            for mapping in mappings:
                iri = mapping.expanded_iri or expand_concept(mapping.concept, self.prefixes)
                self._links.append(ConceptLink(ref, mapping, iri))
        self._by_iri: dict[str, list[ConceptLink]] = {}
        for link in self._links:
            self._by_iri.setdefault(link.expanded_iri, []).append(link)

    @staticmethod
    def _iter_objects(
        model: SemanticModel, model_name: str
    ) -> Iterable[tuple[SemanticObjectRef, list[ExternalConceptMapping]]]:
        yield SemanticObjectRef("model", model_name), model.external_concept_mappings
        for name, obj in model.data_objects.items():
            yield SemanticObjectRef("dataObject", name), obj.external_concept_mappings
        for name, dim in model.dimensions.items():
            yield SemanticObjectRef("dimension", name), dim.external_concept_mappings
        # Declared measures only: synthesized counts never carry mappings.
        for name, meas in model.measures.items():
            yield SemanticObjectRef("measure", name), meas.external_concept_mappings
        for name, met in model.metrics.items():
            yield SemanticObjectRef("metric", name), met.external_concept_mappings
        for name, rule in model.rules.items():
            yield SemanticObjectRef("rule", name), rule.external_concept_mappings

    # -- lookups -------------------------------------------------------------

    @property
    def links(self) -> list[ConceptLink]:
        return list(self._links)

    def expand(self, concept: str) -> str:
        """Absolute IRI for a compact or full concept, using the model's prefixes.

        Raises :class:`~orionbelt.models.concept_links.ConceptIriError` for a
        value the model could not have authored either.
        """
        return expand_concept(concept, self.prefixes)

    def namespace_of(self, iri: str) -> tuple[str | None, str]:
        """``(prefix, namespace)`` for an IRI: the longest declared or built-in
        namespace that covers it, else the IRI up to its last ``#`` or ``/``."""
        best: tuple[str | None, str] | None = None
        for prefix, namespace in self.prefixes.items():
            if iri.startswith(namespace) and (best is None or len(namespace) > len(best[1])):
                best = (prefix, namespace)
        return best if best is not None else (None, _split_namespace(iri))

    def resolve_namespace(self, wanted: str) -> str:
        """The namespace IRI a ``namespace`` filter denotes.

        A declared or built-in prefix name resolves to its namespace; an
        absolute IRI is taken as written. Anything else is refused the way an
        unexpandable concept is, so a typo (``acme`` for ``corp``) is an error
        rather than a silently empty result.
        """
        declared = self.prefixes.get(wanted)
        if declared is not None:
            return declared
        if is_absolute_iri(wanted):
            return wanted
        raise ConceptIriError(
            "UNKNOWN_ONTOLOGY_PREFIX",
            f"'{wanted}' is neither a declared prefix nor an absolute namespace IRI",
            suggestions=sorted(self.prefixes),
        )

    def find(
        self,
        *,
        concept: str | None = None,
        namespace: str | None = None,
        relation: str | None = None,
        types: Iterable[str] | None = None,
    ) -> list[ConceptLink]:
        """Mappings matching every given filter, in model order.

        Raises :class:`~orionbelt.models.concept_links.ConceptIriError` for a
        ``concept`` that cannot expand or a ``namespace`` that is neither a
        known prefix nor an absolute IRI.
        """
        links = self._by_iri.get(self.expand(concept), []) if concept else self._links
        namespace_iri = self.resolve_namespace(namespace) if namespace else None
        wanted_types = set(types) if types else None
        return [
            link
            for link in links
            if (namespace_iri is None or link.expanded_iri.startswith(namespace_iri))
            and (relation is None or link.mapping.relation.value == relation)
            and (wanted_types is None or link.object.type in wanted_types)
        ]

    def namespaces(self) -> list[NamespaceUsage]:
        """Every external namespace the model links into, most used first."""
        usage: dict[tuple[str | None, str], tuple[int, set[SemanticObjectRef]]] = {}
        for link in self._links:
            key = self.namespace_of(link.expanded_iri)
            count, objects = usage.get(key, (0, set()))
            objects.add(link.object)
            usage[key] = (count + 1, objects)
        rows = [
            NamespaceUsage(prefix, namespace, count, len(objects))
            for (prefix, namespace), (count, objects) in usage.items()
        ]
        return sorted(rows, key=lambda row: (-row.mapping_count, row.namespace))

    def unmapped(self, types: Iterable[str] | None = None) -> list[SemanticObjectRef]:
        """Objects in the mappable scope that carry no mapping, in model order."""
        wanted = set(types) if types else set(MAPPABLE_TYPES)
        mapped = {link.object for link in self._links}
        return [ref for ref in self._objects if ref.type in wanted and ref not in mapped]

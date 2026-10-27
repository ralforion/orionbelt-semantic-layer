"""'Did you mean?' suggestions for names a query or model could not resolve."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import get_close_matches
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orionbelt.models.semantic import SemanticModel

#: How alike two names must be (difflib ratio) before one is suggested.
_CUTOFF = 0.6


def dimension_names(model: SemanticModel) -> dict[str, list[str]]:
    """Every dimension with its synonyms, for :func:`suggest_names`."""
    return {name: dim.synonyms for name, dim in model.dimensions.items()}


def measure_names(model: SemanticModel) -> dict[str, list[str]]:
    """Every measure (synthesized counts included) and metric with its synonyms."""
    names = {name: m.synonyms for name, m in model.effective_measures.items()}
    names.update({name: m.synonyms for name, m in model.metrics.items()})
    return names


def artefact_kind(model: SemanticModel, name: str) -> str | None:
    """``"dimension"``, ``"measure"`` or ``"metric"`` when ``name`` is one, else ``None``.

    Lets an error for a name used in the wrong place say where it belongs.
    """
    if name in model.dimensions:
        return "dimension"
    if name in model.effective_measures:
        return "measure"
    if name in model.metrics:
        return "metric"
    return None


def suggest_names(
    name: str, candidates: Mapping[str, Sequence[str]], *, limit: int = 3
) -> list[str]:
    """Candidate names that ``name`` was probably meant to be, best first.

    ``candidates`` maps each name to its synonyms. A case-insensitive match on
    a name or synonym wins outright; otherwise names and synonyms that contain
    ``name`` (or are contained in it), then spellings close to it, are offered.
    A name suggests at most once, and an unrelated name never: no suggestion is
    better than a wrong one.
    """
    wanted = name.strip().lower()
    if not wanted:
        return []
    owner: dict[str, str] = {}
    for candidate, synonyms in candidates.items():
        for alias in (candidate, *synonyms):
            owner.setdefault(alias.lower(), candidate)
    if wanted in owner:
        return [owner[wanted]]
    contained = [
        alias
        for alias in owner
        if min(len(wanted), len(alias)) >= 3 and (wanted in alias or alias in wanted)
    ]
    close = get_close_matches(wanted, list(owner), n=limit, cutoff=_CUTOFF)
    suggestions: list[str] = []
    for alias in (*sorted(contained, key=len), *close):
        if owner[alias] not in suggestions:
            suggestions.append(owner[alias])
    return suggestions[:limit]

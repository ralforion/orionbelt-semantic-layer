"""Fuzzy matching for /find recovery.

Phase A: deterministic — Levenshtein + trigram overlap. No external deps.
Phase B (semantic embeddings) is deferred per ``design/PLAN_agent_api_improvements.md`` §4.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FuzzyMatch:
    """A single fuzzy match candidate."""

    name: str
    kind: str
    score: float
    reason: str


def _levenshtein(a: str, b: str) -> int:
    """Pure-Python Levenshtein distance — small inputs only."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(
                min(
                    curr[j - 1] + 1,
                    prev[j] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = curr
    return prev[-1]


def _normalize(text: str) -> str:
    return text.lower().strip()


def _trigrams(text: str) -> set[str]:
    """3-character sliding window grams of a normalized string."""
    n = _normalize(text)
    if len(n) < 3:
        return {n} if n else set()
    return {n[i : i + 3] for i in range(len(n) - 2)}


def _trigram_score(query: str, candidate: str) -> float:
    """Jaccard similarity of trigrams. 0..1."""
    qg = _trigrams(query)
    cg = _trigrams(candidate)
    if not qg or not cg:
        return 0.0
    overlap = qg & cg
    union = qg | cg
    return len(overlap) / len(union)


def _edit_score(query: str, candidate: str) -> float:
    """Normalized edit-distance score: 1 = identical, 0 = totally different."""
    qn = _normalize(query)
    cn = _normalize(candidate)
    if not qn or not cn:
        return 0.0
    dist = _levenshtein(qn, cn)
    longest = max(len(qn), len(cn))
    return 1.0 - (dist / longest)


def fuzzy_score(query: str, candidate: str) -> tuple[float, str]:
    """Compute the combined fuzzy score and the dominant reason.

    Weighted blend: trigram overlap (0.6) + edit-distance score (0.4).
    Reason is the contributor that scored highest.
    """
    tri = _trigram_score(query, candidate)
    edit = _edit_score(query, candidate)
    score = 0.6 * tri + 0.4 * edit
    if tri >= edit:
        reason = "trigram overlap"
    else:
        qn, cn = _normalize(query), _normalize(candidate)
        dist = _levenshtein(qn, cn)
        reason = f"edit distance: {dist}"
    return score, reason


def fuzzy_search(
    query: str,
    candidates: list[tuple[str, str, list[str]]],
    *,
    threshold: float = 0.5,
    max_results: int = 10,
) -> list[FuzzyMatch]:
    """Return the top fuzzy matches above ``threshold``.

    ``candidates`` is a list of ``(name, kind, synonyms)`` tuples. The query
    is matched against the name and each synonym; the best score wins.
    """
    scored: list[FuzzyMatch] = []
    for name, kind, synonyms in candidates:
        best_score, best_reason = fuzzy_score(query, name)
        for syn in synonyms:
            s, r = fuzzy_score(query, syn)
            if s > best_score:
                best_score, best_reason = s, f"synonym '{syn}', {r}"
        if best_score >= threshold:
            scored.append(
                FuzzyMatch(name=name, kind=kind, score=round(best_score, 3), reason=best_reason)
            )
    scored.sort(key=lambda m: (-m.score, m.name))
    return scored[:max_results]

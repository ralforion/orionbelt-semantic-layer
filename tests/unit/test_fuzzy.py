"""Tests for service.fuzzy — Levenshtein + trigram fuzzy matching."""

from __future__ import annotations

from orionbelt.service.fuzzy import fuzzy_score, fuzzy_search


class TestFuzzyScore:
    def test_identical_strings_score_one(self) -> None:
        score, _ = fuzzy_score("Country", "Country")
        assert score == 1.0

    def test_one_character_off_scores_high(self) -> None:
        score, _ = fuzzy_score("Country", "Countryy")
        assert score > 0.7

    def test_unrelated_strings_score_low(self) -> None:
        score, _ = fuzzy_score("Region", "Sales Tax")
        assert score < 0.3

    def test_case_insensitive(self) -> None:
        s1, _ = fuzzy_score("country", "COUNTRY")
        s2, _ = fuzzy_score("Country", "Country")
        assert s1 == s2 == 1.0


class TestFuzzySearch:
    def _candidates(self) -> list[tuple[str, str, list[str]]]:
        return [
            ("Customer Country", "dimension", []),
            ("Sales Region", "dimension", ["region"]),
            ("Product Category", "dimension", []),
            ("Total Revenue", "measure", []),
        ]

    def test_misspelling_matches_close_candidate(self) -> None:
        results = fuzzy_search("Region", self._candidates())
        names = [m.name for m in results]
        assert "Sales Region" in names

    def test_threshold_filters_garbage(self) -> None:
        results = fuzzy_search("XQZ123", self._candidates(), threshold=0.5)
        assert results == []

    def test_synonym_match_via_fuzzy(self) -> None:
        # "regn" is close to the synonym "region" via trigrams
        results = fuzzy_search("regn", self._candidates(), threshold=0.3)
        assert any(m.name == "Sales Region" for m in results)

    def test_max_results_caps_count(self) -> None:
        large = [(f"Field {i}", "dimension", []) for i in range(20)]
        results = fuzzy_search("Field", large, threshold=0.3, max_results=5)
        assert len(results) <= 5

    def test_results_sorted_by_score_desc(self) -> None:
        results = fuzzy_search("Custome", self._candidates(), threshold=0.3)
        scores = [m.score for m in results]
        assert scores == sorted(scores, reverse=True)

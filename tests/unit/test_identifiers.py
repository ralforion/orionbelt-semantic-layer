"""Tests for the model-name normalization pipeline."""

from __future__ import annotations

import pytest

from orionbelt.models.identifiers import (
    RESERVED_NAMES,
    ModelNameError,
    normalize_model_name,
)


class TestNormalization:
    """Happy-path normalization — input → resolved name."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # Simple lowercase identifiers pass through
            ("sales", "sales"),
            ("Sales", "sales"),
            ("SALES", "sales"),
            ("sales_q4", "sales_q4"),
            ("sales123", "sales123"),
            # Spaces collapse to underscores
            ("My Sales Model", "my_sales_model"),
            ("  trim me  ", "trim_me"),
            ("a    b    c", "a_b_c"),
            # Dashes and dots collapse the same way
            ("sales-q4", "sales_q4"),
            ("commerce.v2", "commerce_v2"),
            ("My-Sales.Model", "my_sales_model"),
            # Multi-separator runs collapse to one underscore
            ("sales--q4..2025", "sales_q4_2025"),
            ("a..b--c  d", "a_b_c_d"),
            # Leading/trailing junk stripped
            ("-sales-", "sales"),
            ("..sales..", "sales"),
            ("__sales__", "sales"),
            (" .--sales--. ", "sales"),
            # _obml suffix stripped (case-insensitive via lowercase step)
            ("commerce.obml", "commerce"),
            ("Sales.OBML", "sales"),
            ("my-model.obml", "my_model"),
            # Filenames with the .obml.yaml convention (caller passes stem)
            ("commerce.obml", "commerce"),
            # Stripping _obml then stripping trailing _ if any
            ("sales._.obml", "sales"),
            # Length boundary
            ("a", "a"),
            ("a" * 63, "a" * 63),
        ],
    )
    def test_normalizes_correctly(self, raw: str, expected: str) -> None:
        assert normalize_model_name(raw, source="test") == expected


class TestRejections:
    """Invalid inputs raise ModelNameError with the source named."""

    def test_empty_string(self) -> None:
        with pytest.raises(ModelNameError, match="empty"):
            normalize_model_name("", source="test")

    def test_only_separators(self) -> None:
        with pytest.raises(ModelNameError, match="empty"):
            normalize_model_name(" . - _ . - ", source="test")

    def test_starts_with_digit(self) -> None:
        with pytest.raises(ModelNameError, match="first character"):
            normalize_model_name("2024_sales", source="test")

    def test_starts_with_digit_after_normalization(self) -> None:
        with pytest.raises(ModelNameError, match="first character"):
            normalize_model_name("-2024-sales", source="test")

    def test_too_long(self) -> None:
        with pytest.raises(ModelNameError, match="exceeds 63"):
            normalize_model_name("a" * 64, source="test")

    def test_disallowed_chars_remain(self) -> None:
        with pytest.raises(ModelNameError, match="disallowed characters"):
            normalize_model_name("sales!q4", source="test")

    def test_unicode_rejected(self) -> None:
        with pytest.raises(ModelNameError, match="disallowed characters"):
            normalize_model_name("café", source="test")

    def test_only_obml_suffix(self) -> None:
        # `obml` is reserved (brand acronym), so "obml"/".obml"/"__obml__" all
        # reject with the reserved-name error rather than empty.
        for raw in ("obml", ".obml", "__obml__"):
            with pytest.raises(ModelNameError, match="reserved"):
                normalize_model_name(raw, source="test")

    def test_none_rejected(self) -> None:
        with pytest.raises(ModelNameError, match="None"):
            normalize_model_name(None, source="test")  # type: ignore[arg-type]


class TestReservedNames:
    @pytest.mark.parametrize(
        "name",
        sorted(n for n in RESERVED_NAMES if n.startswith(tuple("abcdefghijklmnopqrstuvwxyz"))),
    )
    def test_reserved_after_normalization(self, name: str) -> None:
        with pytest.raises(ModelNameError, match="reserved"):
            normalize_model_name(name, source="test")

    def test_reserved_via_uppercase(self) -> None:
        with pytest.raises(ModelNameError, match="reserved"):
            normalize_model_name("MODEL", source="test")

    def test_reserved_via_spaces(self) -> None:
        with pytest.raises(ModelNameError, match="reserved"):
            normalize_model_name("information schema", source="test")

    def test_reserved_via_dot(self) -> None:
        with pytest.raises(ModelNameError, match="reserved"):
            normalize_model_name("pg.catalog", source="test")


class TestErrorMessages:
    """Errors name both the source and the intermediate state."""

    def test_source_in_message(self) -> None:
        with pytest.raises(ModelNameError) as exc:
            normalize_model_name(
                "My Bad!Name",
                source="OBML name: field in /path/to/sales.yaml",
            )
        assert "/path/to/sales.yaml" in str(exc.value)
        assert "My Bad!Name" in str(exc.value)

    def test_intermediate_state_in_message(self) -> None:
        with pytest.raises(ModelNameError) as exc:
            normalize_model_name("sales!q4", source="test")
        # Should show the lowercased intermediate
        assert "sales!q4" in str(exc.value)
        assert "disallowed" in str(exc.value)

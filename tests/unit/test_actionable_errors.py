"""Refusals tell the caller how to repair the query.

An agent that compiles first and asks later only gets anywhere if the refusal
names the cause and the fix: a stable code, a hint, the names it probably
meant, and structured context. These tests pin that on the compiler side and
on the surfaces that report it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from orionbelt.api.services.model_loading import _load_obml
from orionbelt.api.services.query_compilation import compile_query_or_raise
from orionbelt.cli._local import CliError, _translate
from orionbelt.cli.main import _fmt_issue
from orionbelt.compiler.fanout import FanoutError, detect_fanout
from orionbelt.compiler.grain_dedup import GrainDedupUnsupportedError
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import Cardinality, SemanticModel
from orionbelt.models.suggestions import suggest_names
from orionbelt.parser.resolver import _suggest_similar
from orionbelt.service.model_store import ModelStore, ModelValidationError
from tests.conftest import SAMPLE_MODEL_YAML
from tests.unit.test_fanout import _make_model, _make_resolved


def _errors(model: SemanticModel, query: dict[str, Any]) -> list[SemanticError]:
    with pytest.raises(ResolutionError) as excinfo:
        CompilationPipeline().compile(QueryObject.model_validate(query), model, "duckdb")
    return excinfo.value.errors


class TestSuggestNames:
    CANDIDATES = {"Customer Country": ["Nation"], "Revenue": [], "Order Date": []}

    def test_a_case_insensitive_match_wins_outright(self) -> None:
        assert suggest_names("customer country", self.CANDIDATES) == ["Customer Country"]

    def test_a_synonym_suggests_its_name(self) -> None:
        assert suggest_names("nation", self.CANDIDATES) == ["Customer Country"]

    def test_a_contained_name_is_suggested(self) -> None:
        assert suggest_names("Country", self.CANDIDATES) == ["Customer Country"]

    def test_a_misspelling_is_suggested(self) -> None:
        assert suggest_names("Revenu", self.CANDIDATES)[0] == "Revenue"

    def test_an_unrelated_name_suggests_nothing(self) -> None:
        assert suggest_names("Zebra", self.CANDIDATES) == []


class TestUnknownReferences:
    def test_unknown_dimension_suggests_close_names(self, sales_model: SemanticModel) -> None:
        (error,) = _errors(
            sales_model, {"select": {"dimensions": ["Country"], "measures": ["Revenue"]}}
        )
        assert error.code == "UNKNOWN_DIMENSION"
        assert error.suggestions == ["Customer Country"]
        assert error.context == {"dimension": "Country"}
        assert error.hint is None

    def test_a_measure_listed_as_dimension_says_where_it_belongs(
        self, sales_model: SemanticModel
    ) -> None:
        (error,) = _errors(sales_model, {"select": {"dimensions": ["Revenue"]}})
        assert error.code == "UNKNOWN_DIMENSION"
        assert error.hint == "'Revenue' is a measure: list it under select.measures."
        assert error.suggestions == []

    def test_unknown_measure_suggests_close_names(self, sales_model: SemanticModel) -> None:
        (error,) = _errors(sales_model, {"select": {"measures": ["Revenu"]}})
        assert error.code == "UNKNOWN_MEASURE"
        assert error.suggestions[0] == "Revenue"

    def test_a_dimension_listed_as_measure_says_where_it_belongs(
        self, sales_model: SemanticModel
    ) -> None:
        (error,) = _errors(sales_model, {"select": {"measures": ["Customer Country"]}})
        assert error.code == "UNKNOWN_MEASURE"
        assert error.hint == "'Customer Country' is a dimension: list it under select.dimensions."
        assert error.suggestions == []

    def test_a_measure_filtered_in_where_points_to_having(self, sales_model: SemanticModel) -> None:
        (error,) = _errors(
            sales_model,
            {
                "select": {"dimensions": ["Customer Country"], "measures": ["Revenue"]},
                "where": [{"field": "Revenue", "op": "gt", "value": 10}],
            },
        )
        assert error.code == "UNKNOWN_FILTER_FIELD"
        assert error.hint == "'Revenue' is a measure: filter it under 'having'."
        assert error.suggestions == []


class TestFanoutErrors:
    def test_fanout_carries_a_code_and_the_dimensions_that_force_it(self) -> None:
        model = _make_model(cardinality=Cardinality.MANY_TO_MANY)
        resolved = _make_resolved(model, cardinality=Cardinality.MANY_TO_MANY)
        with pytest.raises(FanoutError) as excinfo:
            detect_fanout(resolved, model)
        (error,) = excinfo.value.errors
        assert error.code == "FANOUT"
        assert "('Customer Country')" in error.message
        assert error.context == {
            "measure": "Total Revenue",
            "fromObject": "Orders",
            "toObject": "Customers",
            "dimensions": ["Customer Country"],
        }
        assert excinfo.value.message == error.message

    def test_a_subclass_reports_its_own_code(self) -> None:
        (error,) = GrainDedupUnsupportedError("cannot dedup").errors
        assert error.code == "GRAIN_DEDUP_UNSUPPORTED"


class TestAsText:
    def test_code_message_hint_and_suggestions(self) -> None:
        error = SemanticError(
            code="UNKNOWN_MEASURE",
            message="Unknown measure 'Revenu'",
            hint="Check the name.",
            suggestions=["Revenue", "US Revenue"],
        )
        assert error.as_text() == (
            "[UNKNOWN_MEASURE] Unknown measure 'Revenu' Hint: Check the name. "
            "Did you mean 'Revenue' or 'US Revenue'?"
        )


class _RaisingStore:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def compile_query(self, *_args: object) -> None:
        raise self.exc


class TestRestErrorDetail:
    def _detail(self, exc: Exception) -> Any:
        with pytest.raises(HTTPException) as excinfo:
            compile_query_or_raise(
                store=_RaisingStore(exc),  # type: ignore[arg-type]
                model_id="m",
                query=None,
                dialect="duckdb",
            )
        assert excinfo.value.status_code == 422
        return excinfo.value.detail

    def test_resolution_errors_keep_hint_suggestions_and_context(self) -> None:
        error = SemanticError(
            code="UNKNOWN_MEASURE",
            message="Unknown measure 'Revenu'",
            path="select.measures",
            hint="h",
            suggestions=["Revenue"],
            context={"measure": "Revenu"},
        )
        detail = self._detail(ResolutionError([error]))
        assert detail["errors"] == [
            {
                "code": "UNKNOWN_MEASURE",
                "message": "Unknown measure 'Revenu'",
                "path": "select.measures",
                "severity": "error",
                "hint": "h",
                "context": {"measure": "Revenu"},
                "suggestions": ["Revenue"],
            }
        ]

    def test_fanout_reports_its_code_and_context(self) -> None:
        detail = self._detail(FanoutError("inflated", context={"measure": "Revenue"}))
        assert detail["message"] == "inflated"
        assert detail["errors"][0]["code"] == "FANOUT"
        assert detail["errors"][0]["context"] == {"measure": "Revenue"}


class TestCliObsql:
    def test_translation_failure_keeps_the_suggestion(self, sales_model: SemanticModel) -> None:
        with pytest.raises(CliError) as excinfo:
            _translate(sales_model, 'SELECT "Revenu" FROM sales')
        assert "[UNKNOWN_SELECT_ITEM]" in str(excinfo.value)
        assert "Did you mean 'Revenue'" in str(excinfo.value)


_BROKEN_MODEL = SAMPLE_MODEL_YAML.replace("dataObject: Customers\n", "dataObject: Customer\n", 1)


class TestModelLoadErrors:
    def test_an_unrelated_name_gets_no_model_suggestion(self) -> None:
        assert _suggest_similar("Zebra", ["Customers", "Orders"]) == []

    def test_an_overlapping_check_is_reported_once_with_its_suggestion(self) -> None:
        summary = ModelStore().validate(_BROKEN_MODEL)
        unknown = [e for e in summary.errors if e.code == "UNKNOWN_DATA_OBJECT"]
        assert len(unknown) == 1
        assert unknown[0].suggestions == ["Customers"]

    def test_the_exception_text_carries_path_and_suggestion(self) -> None:
        with pytest.raises(ModelValidationError) as excinfo:
            ModelStore().load_model(_BROKEN_MODEL)
        assert (
            "[UNKNOWN_DATA_OBJECT] Dimension 'Customer Country' references unknown data "
            "object 'Customer' (at dimensions.Customer Country) Did you mean 'Customers'?"
        ) in str(excinfo.value)

    def test_rest_422_keeps_suggestions(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            _load_obml(ModelStore(), _BROKEN_MODEL)
        detail: Any = excinfo.value.detail
        assert excinfo.value.status_code == 422
        (error,) = (e for e in detail["errors"] if e["code"] == "UNKNOWN_DATA_OBJECT")
        assert error["suggestions"] == ["Customers"]
        assert error["path"] == "dimensions.Customer Country"
        assert "Did you mean 'Customers'?" in detail["message"]

    def test_cli_issue_line_shows_the_repair(self) -> None:
        line = _fmt_issue(
            {
                "code": "UNKNOWN_DATA_OBJECT",
                "message": "unknown data object 'Customer'",
                "path": "dimensions.Customer Country",
                "hint": None,
                "suggestions": ["Customers"],
            }
        )
        assert line == (
            "[UNKNOWN_DATA_OBJECT] unknown data object 'Customer' "
            "(at dimensions.Customer Country) Did you mean 'Customers'?"
        )

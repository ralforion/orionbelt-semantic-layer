"""The ``model`` table rules both wire surfaces share."""

from __future__ import annotations

import pytest

from orionbelt.compiler.model_table import (
    expand_model_star,
    is_metadata_probe,
    model_table_columns,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(SAMPLE_MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def test_the_columns_include_the_synthesized_counts(model: SemanticModel) -> None:
    columns = model_table_columns(model)
    assert columns[: len(model.dimensions)] == list(model.dimensions)
    assert set(model.effective_measures) - set(model.measures)
    assert set(model.effective_measures) <= set(columns)
    assert columns[-len(model.metrics) :] == list(model.metrics)


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "sales"."model"',
        "SELECT * FROM sales.model",
        'SELECT m.* FROM "sales"."model" AS m',
        "SELECT * FROM model",
    ],
)
def test_a_lone_star_over_the_model_table_names_every_column(
    model: SemanticModel, sql: str
) -> None:
    expanded = expand_model_star(sql, model)
    assert expanded is not None
    assert "*" not in expanded
    projection = expanded.split(" FROM ")[0]
    assert projection == "SELECT " + ", ".join(f'"{c}"' for c in model_table_columns(model))


def test_the_rest_of_the_statement_is_kept(model: SemanticModel) -> None:
    expanded = expand_model_star(
        'SELECT * FROM "sales"."model" WHERE "Customer Country" = \'US\' '
        'ORDER BY "Total Revenue" DESC LIMIT 5',
        model,
    )
    assert expanded is not None
    assert expanded.endswith(
        'FROM "sales"."model" WHERE "Customer Country" = \'US\' '
        'ORDER BY "Total Revenue" DESC LIMIT 5'
    )


@pytest.mark.parametrize(
    "sql",
    [
        # The OBSQL form: the translator refuses the star there.
        "SELECT * FROM sales",
        # A star beside an artefact: the translator's refusal is precise.
        'SELECT *, "Total Revenue" FROM sales.model',
        'SELECT COUNT(*) FROM "sales"."model"',
        'SELECT "Customer Country" FROM "sales"."model"',
        'SELECT * FROM "sales"."model" JOIN other ON TRUE',
        "SELECT * FROM (SELECT 1) AS model",
        "not sql at all (((",
        # Modifiers change the columns; expanding would silently drop them.
        'SELECT * REPLACE (0 AS "Total Revenue") FROM sales.model',
        'SELECT * EXCLUDE ("Customer Country") FROM sales.model',
        'SELECT m.* EXCLUDE ("Customer Country") FROM sales.model AS m',
    ],
)
def test_anything_else_is_left_alone(model: SemanticModel, sql: str) -> None:
    assert expand_model_star(sql, model) is None


@pytest.mark.parametrize(
    ("sql", "probe"),
    [
        ('SELECT * FROM "sales"."model" WHERE 1=0', True),
        ('SELECT * FROM "sales"."model" LIMIT 0', True),
        ('SELECT * FROM "sales"."model" WHERE false', True),
        ('SELECT * FROM "sales"."model" FETCH FIRST 0 ROWS ONLY', True),
        # FETCH keeps its count elsewhere than LIMIT; reading it as one raised.
        ('SELECT * FROM "sales"."model" FETCH FIRST 1 ROW ONLY', False),
        ('SELECT * FROM "sales"."model" FETCH NEXT ROWS ONLY', False),
        ('SELECT * FROM "sales"."model" LIMIT ALL', False),
        ('SELECT * FROM "sales"."model" WHERE 0 = 1 AND "Customer Country" = \'US\'', True),
        ('SELECT * FROM "sales"."model" WHERE 1=0 OR "Customer Country" = \'US\'', False),
        # Text that only looks like a zero-row clause.
        ('SELECT * FROM "sales"."model" /* LIMIT 0 */ WHERE "Customer Country" = \'US\'', False),
        ('SELECT * FROM "sales"."model" WHERE "Customer Country" = \'LIMIT 0\'', False),
        ('SELECT * FROM "sales"."model" WHERE "Customer Country" = \'WHERE 1=0\'', False),
        ('SELECT * EXCLUDE ("Customer Country") FROM "sales"."model" LIMIT 0', False),
        ('SELECT * FROM "sales"."model"', False),
        ('SELECT "Customer Country" FROM "sales"."model" LIMIT 0', False),
    ],
)
def test_a_zero_row_probe_needs_the_star_and_the_clause(sql: str, probe: bool) -> None:
    assert is_metadata_probe(sql) is probe

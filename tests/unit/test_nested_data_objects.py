"""A data object whose rows come from unnesting a parent's array column.

A repeated column is a table - one nested table per parent row - so it is
modelled as a data object rather than reached through a scalar accessor. That is
what keeps its keys *data* rather than model-time constants, lets a four-field
shape like Google's ``x_Tags`` keep the two fields beyond key/value, and lets a
measure live on it at its own grain.

This module covers the **OBML surface only**: the field, its validation, and its
propagation. Nothing compiles a ``nestedIn`` object to SQL yet; the rendering
matrix and the codegen land separately.

The design and the per-dialect measurements are in
``design/PLAN_nested_data_objects.md``.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import DataObject, NestedSource, UnrenderableDataObjectError
from orionbelt.obsl.exporter import export_obsl
from orionbelt.parser import ReferenceResolver, TrackedLoader
from orionbelt.parser.validator import SemanticValidator

HEAD = """
version: "1.0"
name: nested
dataObjects:
  Charges:
    code: charges
    columns:
      Cost: {code: cost, abstractType: float}
"""
TAIL = """
measures:
  Total Cost:
    columns: [{dataObject: Charges, column: Cost}]
    resultType: float
    aggregation: sum
"""


def _resolve(objects: str) -> tuple[object, list]:
    """Resolve, then run the semantic validator the way ``ModelStore`` does."""
    raw, source_map = TrackedLoader().load_string(HEAD + objects + TAIL)
    model, result = ReferenceResolver().resolve(raw, source_map)
    errors = list(result.errors)
    if result.valid:
        errors.extend(SemanticValidator().validate(model))
    return model, errors


LABELS = """  Charge Labels:
    nestedIn: {dataObject: Charges, column: Labels}
    columns:
      Label Key: {code: Key, abstractType: string}
      Label Value: {code: Value, abstractType: string}
"""


class TestTheSurface:
    def test_a_nested_object_resolves(self) -> None:
        model, errors = _resolve(LABELS)
        assert not errors, errors
        obj = model.data_objects["Charge Labels"]
        assert obj.is_nested
        assert obj.nested_in == NestedSource(data_object="Charges", column="Labels")

    def test_a_dotted_column_addresses_an_array_inside_a_struct(self) -> None:
        """``x_Project.Ancestors`` in the Google export is a repeated record
        inside a nullable record, so the column is a path rather than a name.
        """
        model, errors = _resolve(
            "  Ancestors:\n"
            "    nestedIn: {dataObject: Charges, column: x_Project.Ancestors}\n"
            "    columns: {Display Name: {code: DisplayName, abstractType: string}}\n"
        )
        assert not errors, errors
        assert model.data_objects["Ancestors"].nested_in.column == "x_Project.Ancestors"

    def test_an_ordinary_object_is_not_nested(self) -> None:
        model, _ = _resolve(LABELS)
        assert not model.data_objects["Charges"].is_nested

    def test_code_and_nested_in_together_are_the_supported_case(self) -> None:
        """Not an either/or. An object that can be unnested *and* has a
        flattening view behind it stays queryable on an engine that cannot
        unnest, which is what makes moving a model off hand-written views
        incremental rather than all-at-once.
        """
        model, errors = _resolve(
            "  Charge Credits:\n"
            "    nestedIn: {dataObject: Charges, column: Credits}\n"
            "    code: v_charge_credits\n"
            "    columns: {Credit Type: {code: Type, abstractType: string}}\n"
        )
        assert not errors, errors
        obj = model.data_objects["Charge Credits"]
        assert obj.is_nested and obj.code == "v_charge_credits"

    def test_an_object_with_neither_source_is_refused(self) -> None:
        _, errors = _resolve("  Nowhere:\n    columns: {K: {code: k, abstractType: string}}\n")
        assert errors and "neither" in errors[0].message


class TestWhatTheValidatorRefuses:
    def test_a_parent_that_does_not_exist(self) -> None:
        _, errors = _resolve(
            "  L:\n    nestedIn: {dataObject: Nope, column: Labels}\n"
            "    columns: {K: {code: Key, abstractType: string}}\n"
        )
        assert [e.code for e in errors] == ["UNKNOWN_DATA_OBJECT"]

    def test_an_object_nested_in_itself(self) -> None:
        _, errors = _resolve(
            "  L:\n    nestedIn: {dataObject: L, column: Labels}\n"
            "    columns: {K: {code: Key, abstractType: string}}\n"
        )
        assert [e.code for e in errors] == ["INVALID_NESTED_SOURCE"]

    def test_a_cyclic_chain_never_reaches_a_table(self) -> None:
        _, errors = _resolve(
            "  A:\n    nestedIn: {dataObject: B, column: x}\n"
            "    columns: {K: {code: Key, abstractType: string}}\n"
            "  B:\n    nestedIn: {dataObject: A, column: y}\n"
            "    columns: {K2: {code: Key, abstractType: string}}\n"
        )
        assert {e.code for e in errors} == {"INVALID_NESTED_SOURCE"}
        assert len(errors) == 2

    def test_joining_to_a_nested_object_from_elsewhere(self) -> None:
        """There is no key to join on. A nested object's rows exist only inside
        its parent's, so it can only be reached through that parent - emitting
        SQL for such a join is not possible, so it is refused at load.
        """
        _, errors = _resolve(
            LABELS + "  Other:\n    code: other\n"
            "    columns: {K2: {code: k2, abstractType: string}}\n"
            "    joins: [{joinType: many-to-one, joinTo: Charge Labels,"
            " columnsFrom: [K2], columnsTo: [Label Key]}]\n"
        )
        assert [e.code for e in errors] == ["INVALID_NESTED_SOURCE"]
        assert "no key to join on" in errors[0].message


class TestWhatIsAllowed:
    def test_an_array_inside_an_array(self) -> None:
        """Depth is fine - only a cycle is not. A nested object whose parent is
        itself nested reaches a table by walking up the chain.
        """
        _, errors = _resolve(
            LABELS + "  Label Parts:\n"
            "    nestedIn: {dataObject: Charge Labels, column: Parts}\n"
            "    columns: {Part: {code: Part, abstractType: string}}\n"
        )
        assert not errors, errors

    def test_a_nested_object_joining_onward_to_a_third(self) -> None:
        """The nested object is already in FROM through its parent, so a join it
        declares is an ordinary keyed one and is left alone.
        """
        _, errors = _resolve(
            "  Currencies:\n    code: currencies\n"
            "    columns: {Code: {code: code, abstractType: string}}\n"
            "  Charge Credits:\n"
            "    nestedIn: {dataObject: Charges, column: Credits}\n"
            "    columns: {Currency Code: {code: Currency, abstractType: string}}\n"
            "    joins: [{joinType: many-to-one, joinTo: Currencies,"
            " columnsFrom: [Currency Code], columnsTo: [Code]}]\n"
        )
        assert not errors, errors


def test_nested_source_rejects_unknown_keys() -> None:
    """``extra: forbid``, so a typo is an error rather than a silent no-op."""
    with pytest.raises(ValueError):
        NestedSource(dataObject="Charges", colunm="Labels")  # type: ignore[call-arg]


def test_a_plain_object_still_needs_no_nested_in() -> None:
    obj = DataObject(name="X", code="x", database="", schema="")
    assert obj.nested_in is None and not obj.is_nested


class TestANestedObjectIsNotQueryableYet:
    """No dialect compiles an unnest, so a nested-only object has no table.

    Found in review of #342: without these guards the planners fell through to
    an empty ``code`` and emitted ``FROM "" AS "Charge Labels"`` - reachable on
    any model that adopts the field, because a synthesized count covers every
    countable object and needs no measure declared at all.

    Both guards go when the per-dialect rendering lands.
    """

    def test_no_count_is_synthesized_for_a_nested_only_object(self) -> None:
        model, errors = _resolve(LABELS)
        assert not errors, errors
        assert "Charges Count" in model.effective_measures
        assert "Charge Labels Count" not in model.effective_measures

    def test_selecting_from_one_raises_rather_than_emitting_an_empty_table(self) -> None:
        model, _ = _resolve(LABELS)
        obj = model.data_objects["Charge Labels"]
        with pytest.raises(UnrenderableDataObjectError, match="no dialect compiles"):
            obj.require_table_source()
        with pytest.raises(UnrenderableDataObjectError):
            _ = obj.qualified_code

    def test_the_code_fallback_makes_it_queryable_today(self) -> None:
        """The fallback is not only future-proofing. An object that carries both
        reads its flattening view now, which is the whole point of allowing
        both to be declared.
        """
        model, errors = _resolve(
            "  Charge Labels:\n"
            "    nestedIn: {dataObject: Charges, column: Labels}\n"
            "    code: v_charge_labels\n"
            "    columns: {Label Value: {code: Value, abstractType: string}}\n"
        )
        assert not errors, errors
        assert "Charge Labels Count" in model.effective_measures
        sql = (
            CompilationPipeline()
            .compile(
                QueryObject(select=QuerySelect(dimensions=[], measures=["Charge Labels Count"])),
                model,
                "duckdb",
            )
            .sql
        )
        assert '"v_charge_labels"' in sql and 'FROM ""' not in sql, sql

    def test_a_query_that_never_touches_it_is_unaffected(self) -> None:
        model, _ = _resolve(LABELS)
        sql = (
            CompilationPipeline()
            .compile(
                QueryObject(select=QuerySelect(dimensions=[], measures=["Total Cost"])),
                model,
                "duckdb",
            )
            .sql
        )
        assert '"charges"' in sql


def test_the_rdf_export_carries_the_nested_source() -> None:
    """``/graph`` and SPARQL are a surface of their own: the ontology declaring
    the class is not enough if the exporter never emits an instance of it.
    Found in review of #342.
    """
    model, errors = _resolve(LABELS)
    assert not errors, errors
    graph = export_obsl(model, "demo")
    rows = list(
        graph.query(
            "PREFIX obsl: <https://ralforion.com/ns/obsl#> "
            "SELECT ?o ?parent ?col WHERE { ?o obsl:nestedIn ?ns . "
            "?ns obsl:nestedInObject ?parent ; obsl:nestedInColumn ?col }"
        )
    )
    assert len(rows) == 1, rows
    obj, parent, column = rows[0]
    assert str(obj).endswith("charge-labels")
    assert str(parent).endswith("charges")
    assert str(column) == "Labels"

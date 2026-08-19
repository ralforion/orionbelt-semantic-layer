"""Putting a nested data object into a FROM clause.

A ``nestedIn`` object's rows are an array column on its parent, so it reaches
the FROM clause as an unnest rather than as a join. The planners walk one list
of join steps and do not otherwise care which is which, so the branch lives here
once and both the star and the raw planner call it.

Two things this module owns beyond the fragment itself.

**Which source is used.** An object may declare ``nestedIn`` *and* ``code``.
The unnest wins wherever the dialect has a FROM-clause form; where it does not -
Dremio, whose ``FLATTEN`` is a projection function - the table is read instead.
The choice has to be made while the plan is built rather than when it is
rendered, because the two produce different clauses, so it is made here and
reported as a warning: "why is this query different on Dremio" should not
require reading generated SQL.

**Where the parent is.** An unnest names its parent, so the parent has to be in
scope already. That is guaranteed rather than checked: the containment edge runs
parent to child, a nested object is never a base object
(:meth:`~orionbelt.compiler.graph.JoinGraph._unnest_root`), and the planners
emit steps in path order.

Each dialect's fragment is executed against that dialect in
``tests/integration/drift/vendor_exec/test_unnest_render_exec.py``, and the
whole plan this module assembles in ``test_nested_plan_exec.py`` beside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import JoinType as ASTJoinType
from orionbelt.ast.nodes import Unnest
from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.dialect.base import UnsupportedNestedAccessError
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import DataObject
from orionbelt.models.warnings import WarningCode, warning

if TYPE_CHECKING:
    from collections.abc import Callable

    from orionbelt.dialect.base import Dialect


def build_unnest(step: JoinStep, obj: DataObject, dialect: Dialect | None) -> Unnest:
    """The AST fragment that unnests *obj* out of its parent's array column.

    ``columns`` carries the child's declared shape because MySQL's
    ``JSON_TABLE`` extracts a declared shape rather than inferring one; the
    other six engines hand back the element and let a field reference read it.
    A computed column has no physical field to extract and is left out.
    """
    assert obj.nested_in is not None

    def sql_type(abstract: str | None) -> str:
        return dialect.nested_column_type(abstract) if dialect else (abstract or "string")

    return Unnest(
        parent_alias=step.from_object,
        column=obj.nested_in.column,
        alias=step.to_object,
        columns=tuple(
            (column.code, sql_type(column.abstract_type))
            for column in obj.columns.values()
            if column.code
        ),
        # An empty array keeps its parent row. There is no model surface for
        # the inner form: measured on a real GCP billing export, 61% of charges
        # carry no labels at all and the inner form drops 95% of the spend.
        outer=step.join_type is not ASTJoinType.INNER,
    )


def emit_join_step(
    *,
    builder: QueryBuilder,
    step: JoinStep,
    new_object: str,
    obj: DataObject,
    graph: JoinGraph,
    qualify: Callable[[DataObject], str],
    dialect: Dialect | None = None,
    warnings: list[SemanticError] | None = None,
) -> None:
    """Add *new_object* to the FROM clause, as an unnest or an ordinary join."""
    if step.nested and new_object == step.to_object and obj.nested_in is not None:
        if dialect is None or dialect.capabilities.supports_from_unnest:
            builder.unnest(build_unnest(step, obj, dialect))
            return
        _warn_fallback(warnings, dialect, new_object, obj)

    builder.join(
        table=qualify(obj),
        on=graph.build_join_condition(step),
        join_type=step.join_type,
        alias=new_object,
    )


def _warn_fallback(
    warnings: list[SemanticError] | None,
    dialect: Dialect,
    name: str,
    obj: DataObject,
) -> None:
    """Refuse or announce the ``code`` fallback on a dialect that cannot unnest.

    Refused when there is nothing to fall back to, and when the fallback table
    has no join to its parent: a flattening view is a separate table, so the
    containment the unnest relied on is exactly what it destroys, and a key is
    needed to put it back. Silence would be worse than either - the two sources
    are not guaranteed to agree, since a view can filter, rename or aggregate,
    and nothing checks that it matches the array it stands in for.
    """
    assert obj.nested_in is not None
    if not obj.code:
        raise UnsupportedNestedAccessError(dialect.name, name)
    if not obj.joins or not any(j.join_to == obj.nested_in.data_object for j in obj.joins):
        raise UnsupportedNestedAccessError(
            dialect.name,
            name,
            detail=(
                f"Its 'code' fallback '{obj.code}' is a table of its own, so it needs a "
                f"declared join back to '{obj.nested_in.data_object}' - unnesting needs no "
                f"key because the elements are inside the parent's row, and a flattening "
                f"view is what destroys that."
            ),
        )
    if warnings is not None:
        warnings.append(
            warning(
                code=WarningCode.NESTED_SOURCE_FALLBACK,
                message=(
                    f"Data object '{name}' was read from its table '{obj.code}' rather than "
                    f"by unnesting '{obj.nested_in.data_object}.{obj.nested_in.column}': "
                    f"dialect '{dialect.name}' has no FROM-clause unnest."
                ),
                hint=(
                    "The two sources are not guaranteed to agree - a view can filter, "
                    "rename or aggregate - so check the fallback matches the array it "
                    "stands in for."
                ),
                context={
                    "dataObject": name,
                    "dialect": dialect.name,
                    "source": "code",
                    "code": obj.code,
                },
            )
        )

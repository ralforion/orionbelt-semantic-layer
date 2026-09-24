"""Role objects: one aliased copy of a data object per dimension role.

A dimension that sets ``via`` and ``pathName`` reads its data object through one
named join, such as the support employee of an order rather than the sales one.
Two roles of the same table in one query need the table joined twice, under two
aliases, and every planner, filter and wrapper aliases a joined table by its
data object's name. So each role becomes a data object of its own for the
duration of a compile: a copy of the target named after the role, joined from
``via`` by the role's join columns, with the role's dimensions pointing at it.
Everything downstream then handles the second join the way it handles any
other, without knowing roles exist.

The copies are derived, never persisted, in the same way as the synthesized
count measures: the authored model keeps one ``Employees`` and the YAML, OSI and
ontology exports never see the role objects.

A role object is a leaf. It carries none of the target's own joins, because
those would give every object beyond the target a second route from the fact,
and a query reaching one by two equally short routes is refused as ambiguous.
"""

from __future__ import annotations

from orionbelt.models.expressions import rename_object_references
from orionbelt.models.semantic import (
    DataObject,
    DataObjectJoin,
    Dimension,
    SemanticModel,
)


def role_object_names(model: SemanticModel) -> dict[tuple[str, str, str], str]:
    """``(via, dataObject, pathName)`` of each role to its data object name.

    The name is also the role's SQL alias: ``<dataObject>__<via>__<pathName>``.
    It names the source as well as the path, since ``pathName`` is unique only
    per ``(source, target)`` pair. Object names and path names may themselves
    contain ``__``, so two roles can spell the same name, and so can an authored
    data object; a later claimant gets a ``__2``, ``__3``... suffix, because two
    roles sharing an alias would silently read one join for both.
    """
    names: dict[tuple[str, str, str], str] = {}
    taken = set(model.data_objects)
    for dim in model.dimensions.values():
        if role_join(model, dim) is None:
            continue
        assert dim.via is not None and dim.path_name is not None
        key = (dim.via, dim.view, dim.path_name)
        if key in names:
            continue
        base = f"{dim.view}__{dim.via}__{dim.path_name}"
        name, suffix = base, 2
        while name in taken:
            name, suffix = f"{base}__{suffix}", suffix + 1
        taken.add(name)
        names[key] = name
    return names


def role_targets(model: SemanticModel) -> dict[str, str]:
    """Each role object's name to the authored data object it copies."""
    return {name: target for (_, target, _), name in role_object_names(model).items()}


def role_join(model: SemanticModel, dim: Dimension) -> DataObjectJoin | None:
    """The join a role dimension reads through, or ``None`` if it is not one.

    Primary or secondary alike: naming the primary path pins the dimension to it
    even when a query's ``usePathNames`` swaps the pair's join for the others.
    """
    if dim.via is None or dim.path_name is None:
        return None
    source = model.data_objects.get(dim.via)
    if source is None:
        return None
    return next(
        (j for j in source.joins if j.join_to == dim.view and j.path_name == dim.path_name),
        None,
    )


def _role_object(target: DataObject, name: str) -> DataObject:
    """A leaf copy of *target* under *name*.

    A computed column naming its own object explicitly, ``{[Employees].[First]}``
    rather than ``{First}``, is repointed at the copy; left alone it would read
    the table under the other role's alias.
    """
    columns = {
        col_name: (
            column.model_copy(
                update={
                    "expression": rename_object_references(column.expression, target.name, name)
                }
            )
            if column.expression
            else column
        )
        for col_name, column in target.columns.items()
    }
    return target.model_copy(
        update={"name": name, "columns": columns, "joins": [], "countable": False}
    )


def expand_role_objects(model: SemanticModel) -> SemanticModel:
    """*model* with a role object per distinct role its dimensions name.

    Returns *model* itself when no dimension names a role, so a model without
    roles compiles exactly as it did. A dimension whose role join does not exist
    is left untouched: the validator reports it at load.
    """
    names = role_object_names(model)
    if not names:
        return model
    data_objects = dict(model.data_objects)
    dimensions = dict(model.dimensions)
    for dim_name, dim in model.dimensions.items():
        join = role_join(model, dim)
        if join is None:
            continue
        assert dim.via is not None and dim.path_name is not None
        name = names[(dim.via, dim.view, dim.path_name)]
        if name not in data_objects:
            data_objects[name] = _role_object(model.data_objects[dim.view], name)
            source = data_objects[dim.via]
            role_edge = join.model_copy(
                update={"join_to": name, "secondary": False, "path_name": None}
            )
            data_objects[dim.via] = source.model_copy(update={"joins": [*source.joins, role_edge]})
        dimensions[dim_name] = dim.model_copy(update={"view": name})
    return model.model_copy(update={"data_objects": data_objects, "dimensions": dimensions})

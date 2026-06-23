"""Raw-mode field resolution extracted from ``QueryResolver``.

Functions take the owning ``QueryResolver`` as their first argument
(``resolver``); ``QueryResolver`` keeps one-line delegators so its public
surface is unchanged. Pure code movement — no behaviour change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.models.errors import SemanticError

if TYPE_CHECKING:
    from orionbelt.compiler.resolution import QueryResolver, _ResolutionContext


def resolve_raw_field(resolver: QueryResolver, ctx: _ResolutionContext, ref: str) -> None:
    """Resolve a ``DataObject.Column`` reference for raw-mode projection.

    Errors are accumulated in the resolution context (raised at the end).
    """
    from orionbelt.compiler.resolution import ResolvedField

    if "." not in ref:
        ctx.errors.append(
            SemanticError(
                code="RAW_FIELD_INVALID_REF",
                message=(
                    f"Raw-mode field '{ref}' must be a qualified 'DataObject.Column' reference"
                ),
                path="select.fields",
            )
        )
        return

    obj_name, col_name = ref.split(".", 1)
    obj_name = obj_name.strip()
    col_name = col_name.strip()
    obj = ctx.model.data_objects.get(obj_name)
    if obj is None:
        ctx.errors.append(
            SemanticError(
                code="RAW_FIELD_UNKNOWN_OBJECT",
                message=f"Raw-mode field '{ref}' references unknown data object '{obj_name}'",
                path="select.fields",
            )
        )
        return
    column = obj.columns.get(col_name)
    if column is None:
        ctx.errors.append(
            SemanticError(
                code="RAW_FIELD_UNKNOWN_COLUMN",
                message=(
                    f"Raw-mode field '{ref}' references unknown column "
                    f"'{col_name}' on data object '{obj_name}'"
                ),
                path="select.fields",
            )
        )
        return

    ctx.result.fields.append(
        ResolvedField(
            object_name=obj_name,
            column_name=col_name,
            source_column=column.code,
            alias=ref,
        )
    )
    ctx.result.required_objects.add(obj_name)

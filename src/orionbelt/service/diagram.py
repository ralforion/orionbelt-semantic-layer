"""Diagram generation from semantic models."""

from __future__ import annotations

from orionbelt.models.semantic import Cardinality, SemanticModel


def _entity_ref(name: str) -> str:
    """Render an entity reference for Mermaid's erDiagram.

    Mermaid allows entity names to be double-quoted, so labels containing
    spaces (e.g. ``Client Complaints``) round-trip without any munging.
    Plain identifiers stay unquoted.
    """
    if any(c in name for c in (" ", "-")):
        return f'"{name}"'
    return name


def _join_label(label: str) -> str:
    """Sanitize a join-edge label for Mermaid.

    Mermaid's erDiagram parser treats the post-``:`` label as a quoted
    string but escapes inside the quotes are limited. Spaces and most
    punctuation are fine; double-quotes are stripped to avoid breaking
    the surrounding quoting.
    """
    return label.replace('"', "")


def generate_mermaid_er(
    model: SemanticModel,
    *,
    show_columns: bool = True,
    theme: str = "default",
) -> str:
    """Generate a Mermaid ER diagram from a parsed :class:`SemanticModel`.

    Returns the raw Mermaid script (without markdown fences).
    The diagram is rendered left-to-right so that "many" entities appear on the
    left and "one" targets on the right.  Secondary joins use dotted lines.

    *theme* is passed through to the Mermaid ``%%{init}%%`` directive
    (e.g. ``"dark"``, ``"neutral"``, ``"default"``).

    Naming strategy: entity references use the model's data-object label
    (double-quoted when they contain spaces). Attribute identifiers use
    each column's physical ``code`` — those are space-free by definition,
    so no name munging is needed and the diagram authentically shows the
    underlying database schema. Mermaid's ER grammar disallows spaces in
    attribute names, which is why we don't render the business label
    here; the spaced label still appears in dropdowns and query results.
    """
    # Collect FK columns (used in join columnsFrom)
    fk_cols: dict[str, set[str]] = {}
    for obj_name, obj in model.data_objects.items():
        for join in obj.joins:
            for fk_col in join.columns_from:
                fk_cols.setdefault(obj_name, set()).add(fk_col)

    # Only override useMaxWidth so the SVG can be wider than its container
    # (the host #er-diagram has overflow:auto for horizontal scroll). Don't
    # tweak fontSize / entityPadding — Mermaid measures column widths with
    # the same font config it renders with, so the moment we override one
    # without matching the other, attribute text clips at the measured
    # width.
    init_cfg = (
        "{'theme': '" + theme + "', "
        "'er': {'useMaxWidth': false}}"
    )
    lines: list[str] = [
        "%%{init: " + init_cfg + "}%%",
        "erDiagram",
        "    direction LR",
    ]

    # Entity definitions
    for obj_name, obj in model.data_objects.items():
        ent_ref = _entity_ref(obj_name)
        if show_columns and obj.columns:
            lines.append(f"    {ent_ref} {{")
            obj_fks = fk_cols.get(obj_name, set())
            for col_name, col in obj.columns.items():
                # Use the physical code as the Mermaid identifier so we
                # never have to substitute spaces — and the diagram
                # reflects the underlying DB column.
                attr_id = col.code or col_name.replace(" ", "")
                if col.primary_key:
                    marker = " PK"
                elif col_name in obj_fks:
                    marker = " FK"
                else:
                    marker = ""
                lines.append(f"        {col.abstract_type.value} {attr_id}{marker}")
            lines.append("    }")

    lines.append("")

    # Relationships from join definitions
    for obj_name, obj in model.data_objects.items():
        from_ref = _entity_ref(obj_name)
        for join in obj.joins:
            to_ref = _entity_ref(join.join_to)

            # Dotted line for secondary joins, solid for primary
            sep = ".." if join.secondary else "--"

            if join.join_type == Cardinality.ONE_TO_ONE:
                rel = f"||{sep}||"
            elif join.join_type == Cardinality.MANY_TO_MANY:
                rel = f"}}o{sep}o{{"
            else:  # many-to-one
                rel = f"}}o{sep}||"

            # Relationship label — keep the business label (with spaces)
            # so the edge text reads naturally.
            if join.path_name:
                label = join.path_name
            elif join.columns_from:
                label = join.columns_from[0]
            else:
                label = "joins"

            lines.append(f'    {from_ref} {rel} {to_ref} : "{_join_label(label)}"')

    return "\n".join(lines)

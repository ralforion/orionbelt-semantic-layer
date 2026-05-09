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


def _attribute_id(name: str) -> str:
    """Camelcase a business label for use as a Mermaid attribute identifier.

    Mermaid's ER grammar (the version Gradio ships) only accepts word-style
    attribute names — no spaces, no quoted strings. Convert "Sales ID" →
    "SalesID" rather than the underscored "Sales_ID" so the rendered
    identifier reads naturally. The original spaced label is still shown
    via the attribute's comment column.
    """
    cleaned = name.replace("-", " ")
    parts = [p for p in cleaned.split(" ") if p]
    if not parts:
        return "_"
    joined = "".join(parts)
    safe = "".join(c for c in joined if c.isalnum() or c == "_")
    return safe or "_"


def _comment(label: str) -> str:
    """Sanitize a string for use inside a Mermaid double-quoted comment."""
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
    a camelCased form of the column label so the rendered name reads as
    a single word ("Sales ID" → ``SalesID``) — Mermaid's ER grammar only
    accepts word-style attribute names.
    """
    # Collect FK columns (used in join columnsFrom)
    fk_cols: dict[str, set[str]] = {}
    for obj_name, obj in model.data_objects.items():
        for join in obj.joins:
            for fk_col in join.columns_from:
                fk_cols.setdefault(obj_name, set()).add(fk_col)

    # Override useMaxWidth on both the er and flowchart configs (Mermaid
    # falls back to flowchart's value for some ER layout decisions) so the
    # SVG renders at its natural width rather than being squashed into
    # the parent container — the host #er-diagram has overflow:auto for
    # horizontal scroll, so wider-than-viewport diagrams scroll cleanly
    # instead of clipping edge labels.
    init_cfg = (
        "{'theme': '" + theme + "', "
        "'flowchart': {'useMaxWidth': false}, "
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
                # Identifier: camelCased business label (no spaces).
                attr_id = _attribute_id(col_name)
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

            lines.append(f'    {from_ref} {rel} {to_ref} : "{_comment(label)}"')

    return "\n".join(lines)

"""Diagram generation from semantic models."""

from __future__ import annotations

from orionbelt.models.semantic import Cardinality, SemanticModel


def _sanitize_id(name: str) -> str:
    """Make a name safe for use as a Mermaid ER entity/attribute identifier."""
    s = name.replace(" ", "_").replace("-", "_")
    return "".join(c for c in s if c.isalnum() or c == "_")


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
    """
    # Collect FK columns (used in join columnsFrom)
    fk_cols: dict[str, set[str]] = {}
    for obj_name, obj in model.data_objects.items():
        for join in obj.joins:
            for fk_col in join.columns_from:
                fk_cols.setdefault(obj_name, set()).add(fk_col)

    # Inject ER-specific config to mitigate Mermaid's attribute-column
    # clipping in dense diagrams: a slightly larger fontSize + explicit
    # padding give the renderer more headroom when it auto-sizes columns,
    # and useMaxWidth=false stops the parent container from squashing the
    # SVG below its natural width (rely on the host element's overflow:auto
    # for horizontal scroll instead).
    init_cfg = (
        "{'theme': '" + theme + "', "
        "'er': {'fontSize': 14, 'entityPadding': 18, "
        "'minEntityWidth': 220, 'minEntityHeight': 80, "
        "'useMaxWidth': false}}"
    )
    lines: list[str] = [
        "%%{init: " + init_cfg + "}%%",
        "erDiagram",
        "    direction LR",
    ]

    # Entity definitions
    for obj_name, obj in model.data_objects.items():
        safe_name = _sanitize_id(obj_name)
        if show_columns and obj.columns:
            lines.append(f"    {safe_name} {{")
            obj_fks = fk_cols.get(obj_name, set())
            for col_name, col in obj.columns.items():
                safe_col = _sanitize_id(col_name)
                # PK takes precedence over FK when a column is both
                if col.primary_key:
                    marker = " PK"
                elif col_name in obj_fks:
                    marker = " FK"
                else:
                    marker = ""
                lines.append(f"        {col.abstract_type.value} {safe_col}{marker}")
            lines.append("    }")

    lines.append("")

    # Relationships from join definitions
    for obj_name, obj in model.data_objects.items():
        safe_from = _sanitize_id(obj_name)
        for join in obj.joins:
            safe_to = _sanitize_id(join.join_to)

            # Dotted line for secondary joins, solid for primary
            sep = ".." if join.secondary else "--"

            if join.join_type == Cardinality.ONE_TO_ONE:
                rel = f"||{sep}||"
            elif join.join_type == Cardinality.MANY_TO_MANY:
                rel = f"}}o{sep}o{{"
            else:  # many-to-one
                rel = f"}}o{sep}||"

            # Relationship label
            if join.path_name:
                label = _sanitize_id(join.path_name)
            elif join.columns_from:
                label = _sanitize_id(join.columns_from[0])
            else:
                label = "joins"

            lines.append(f'    {safe_from} {rel} {safe_to} : "{label}"')

    return "\n".join(lines)

"""Auto-synthesized row-count measures.

Every countable :class:`~orionbelt.models.semantic.DataObject` bound to a model
yields a grain-anchored count measure ``<object>.count``. The count rides the
existing measure machinery: it is an ordinary ``COUNT`` aggregation anchored on
the object (via a column-less :class:`DataColumnRef`), so the star / CFL planners
and the fan-out guard treat it like any declared measure. No new grain concept
and no ad-hoc in-query aggregation are introduced — the count is a *named,
governed* measure.

Design (see ``design/PLAN_synthesized_count_measures.md``):

* **D1** — auto-synthesize one count per countable object.
* **D2** — anchored ``COUNT(*)``; ``result_type = int`` so the formatter skips
  the ``CAST(... AS DECIMAL(p,s))`` path (and ``COUNT`` already infers bigint).
* **D4** — a declared measure whose id equals ``<object>.count`` wins; synthesis
  steps aside (the self-fanning escape hatch, e.g. ``COUNT(DISTINCT sale_id)``).
* **D5** — per-object ``countable`` opt-out; model-level ``exposeCounts`` default;
  label precedence ``countLabel`` > ``countLabelPattern`` > ``"{object} Count"``.

The synthesized measures are **not** persisted on the model — they are computed
on demand so they never roundtrip through YAML/OSI and the model stays byte-clean
(the knobs roundtrip; the derived measures regenerate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.models.semantic import (
    AggregationType,
    DataColumnRef,
    DataType,
    Measure,
)

if TYPE_CHECKING:
    from orionbelt.models.semantic import DataObject, SemanticModel

DEFAULT_COUNT_PATTERN = "{object} Count"

#: Suffix that turns a data-object reference into its synthesized count id.
COUNT_SUFFIX = ".count"


def count_measure_id(object_key: str) -> str:
    """The stable machine key for an object's synthesized count measure."""
    return f"{object_key}{COUNT_SUFFIX}"


def count_label(object_key: str, obj: DataObject, model: SemanticModel) -> str:
    """Resolve the display label for an object's count measure (D5 precedence).

    ``{object}`` interpolates the object's display label (``label`` falls back to
    the reference key), so ``"# {object}"`` over a technical ``fact_sales`` labeled
    ``Sales`` reads ``"# Sales"``. Uses ``str.replace`` (not ``.format``) so a stray
    brace in a free-form ``countLabel`` override never raises.
    """
    template = (
        obj.count_label or getattr(model, "count_label_pattern", None) or DEFAULT_COUNT_PATTERN
    )
    display = obj.label or object_key
    return template.replace("{object}", display)


def synthesize_count_measure(object_key: str, obj: DataObject, model: SemanticModel) -> Measure:
    """Build the anchored ``COUNT`` measure for one countable data object.

    The single column-less :class:`DataColumnRef` anchors the count on the object
    (so ``_get_measure_source_objects`` picks it up and base-object selection and
    fan-out detection behave as for a declared measure) while the resolver emits
    ``COUNT(*)`` because the ref carries no column.
    """
    return Measure(
        label=count_label(object_key, obj, model),
        columns=[DataColumnRef(view=object_key)],
        aggregation=AggregationType.COUNT,
        result_type=DataType.INT,
        data_type="integer",
        description=f"Row count of {obj.label or object_key}.",
    )


def synthesize_count_measures(model: SemanticModel) -> dict[str, Measure]:
    """Return synthesized count measures keyed by ``<object>.count``.

    Honors ``exposeCounts`` (model), ``countable`` (object), and the declared-wins
    override (D4): an id already present in ``model.measures`` is skipped.
    """
    if not getattr(model, "expose_counts", True):
        return {}
    out: dict[str, Measure] = {}
    for object_key, obj in model.data_objects.items():
        if not obj.countable:
            continue
        mid = count_measure_id(object_key)
        if mid in model.measures:
            continue
        out[mid] = synthesize_count_measure(object_key, obj, model)
    return out

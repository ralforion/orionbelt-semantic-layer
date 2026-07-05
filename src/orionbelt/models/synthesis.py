"""Auto-synthesized row-count measures.

Every countable :class:`~orionbelt.models.semantic.DataObject` bound to a model
yields a grain-anchored count measure whose **name and label are the same
human string** (default ``"Sales Count"``), exactly like a declared measure
(``"Order Count"``, ``"Total Revenue"``). The count rides the existing measure
machinery: it is an ordinary ``COUNT`` aggregation anchored on the object (via a
column-less :class:`DataColumnRef`), so the star / CFL planners and the fan-out
guard treat it like any declared measure. No new grain concept and no ad-hoc
in-query aggregation are introduced -- the count is a *named, governed* measure.

Design (see ``design/PLAN_synthesized_count_measures.md``):

* **D1** -- auto-synthesize one count per countable object.
* **D2** -- anchored ``COUNT(*)``; ``result_type = int`` so the formatter skips
  the ``CAST(... AS DECIMAL(p,s))`` path (and ``COUNT`` already infers bigint).
* **D4** -- a declared measure whose name equals the count's name wins; synthesis
  steps aside (the self-fanning escape hatch, e.g. ``COUNT(DISTINCT sale_id)``).
* **D5** -- per-object ``countable`` opt-out; model-level ``exposeCounts`` default;
  name/label precedence ``countLabel`` > ``countLabelPattern`` > ``"{object} Count"``.

The measure **name is the resolved label** (id == label): consistent with
declared measures, so ``select.measures: ["Sales Count"]`` reads naturally next
to ``["Order Count"]``. The synthesized measures are **not** persisted on the
model -- they are computed on demand so they never roundtrip through YAML/OSI and
the model stays byte-clean (the knobs roundtrip; the derived measures regenerate).
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


def count_pattern_error(pattern: object) -> str | None:
    """Return an error message if ``pattern`` is not a valid count-label pattern.

    Valid = a string whose only replacement field is ``{object}``. Returns
    ``None`` when valid. Shared by the Pydantic field validator (which raises)
    and the OBML resolver (which records a structured error) so both surfaces
    agree on what a legal ``countLabelPattern`` is.
    """
    if not isinstance(pattern, str):
        return "countLabelPattern must be a string"
    import string

    for _text, field_name, _spec, _conv in string.Formatter().parse(pattern):
        if field_name is None:
            continue
        if field_name != "object":
            return (
                f"countLabelPattern may only contain the '{{object}}' token, got '{{{field_name}}}'"
            )
    return None


def count_label(object_key: str, obj: DataObject, pattern: str | None = None) -> str:
    """Resolve the name/label for an object's count measure (D5 precedence).

    Precedence: per-object ``countLabel`` > model ``countLabelPattern`` (passed in
    as ``pattern``) > built-in default ``"{object} Count"``. ``{object}``
    interpolates the object's display label (``label`` falls back to the
    reference key), so ``"# {object}"`` over a technical ``fact_sales`` labeled
    ``Sales`` reads ``"# Sales"``. Uses ``str.replace`` (not ``.format``) so a
    stray brace in a free-form ``countLabel`` override never raises.

    The returned string is BOTH the measure's queryable id and its display label.
    """
    template = obj.count_label or pattern or DEFAULT_COUNT_PATTERN
    display = obj.label or object_key
    return template.replace("{object}", display)


def model_count_pattern(model: SemanticModel) -> str:
    """The model's count label pattern, defaulted."""
    return getattr(model, "count_label_pattern", None) or DEFAULT_COUNT_PATTERN


def synthesize_count_measure(object_key: str, obj: DataObject, name: str) -> Measure:
    """Build the anchored ``COUNT`` measure for one countable data object.

    ``name`` is the resolved count label (id == label). The single column-less
    :class:`DataColumnRef` anchors the count on the object (so
    ``_get_measure_source_objects`` picks it up and base-object selection and
    fan-out detection behave as for a declared measure) while the resolver emits
    ``COUNT(*)`` because the ref carries no column.
    """
    return Measure(
        label=name,
        columns=[DataColumnRef(view=object_key)],
        aggregation=AggregationType.COUNT,
        result_type=DataType.INT,
        data_type="integer",
        description=f"Row count of {obj.label or object_key}.",
    )


def synthesize_count_measures(model: SemanticModel) -> dict[str, Measure]:
    """Return synthesized count measures keyed by their resolved name (== label).

    Honors ``exposeCounts`` (model), ``countable`` (object), and the declared-wins
    override (D4): a name already present in ``model.measures`` is skipped. When
    two objects resolve to the same count name, the first one wins here; the
    collision is reported by the semantic validator.
    """
    if not getattr(model, "expose_counts", True):
        return {}
    pattern = model_count_pattern(model)
    out: dict[str, Measure] = {}
    for object_key, obj in model.data_objects.items():
        if not obj.countable:
            continue
        name = count_label(object_key, obj, pattern)
        if name in model.measures or name in out:
            continue
        out[name] = synthesize_count_measure(object_key, obj, name)
    return out

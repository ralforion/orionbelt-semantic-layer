# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""The Apache Ossie ``0.3.0.dev0`` document shape, at the converter's boundary.

Spec ``0.3.0.dev0`` (apache/ossie#691 and the changes before it) differs from
``0.2.0.dev0`` in three ways the converter meets:

- an expression may be written as ``expression: {sql: ...}``, the portable
  ``OSSIE_SQL_2026`` form, authoritative over an ``OSSIE_SQL_2026`` entry in
  ``dialects``, which became optional;
- a dataset may declare its own ``metrics``, whose expressions name the
  dataset's fields unqualified (and its source columns as ``source.column``);
- ``custom_extensions`` (``data`` a JSON string) became ``nonportable_extensions``
  (``data`` a JSON object).

:func:`to_internal` rewrites a document into the shape the rest of the import
reads, so a ``0.2.0.dev0`` document and a ``0.3.0.dev0`` one take the same path.
It goes by shape, not by the declared version, so a document mixing the two is
read too. :func:`to_v03` turns the export's extensions into the ``0.3.0.dev0``
form.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from typing import Any

from sqlglot import expressions as exp

from osi_orionbelt._common import _OSI_DIALECT_TO_SQLGLOT
from osi_orionbelt._ossie_parse import parse_sql

_OSSIE_SQL = "OSSIE_SQL_2026"
# A dataset-scoped metric may read a column of the dataset's source that no
# field exposes as ``source.<column>``.
_SOURCE_QUALIFIER = "source"
# Set by the import on each lifted metric: ``{"dataset": ..., "metric": ...}``,
# the metric as its dataset declared it.
SCOPE_KEY = "_ossie_scope"


def to_internal(doc: dict[str, Any], warn: Callable[[str], None]) -> dict[str, Any]:
    """A copy of *doc* in the ``0.2.0.dev0`` shape the import reads."""
    doc = copy.deepcopy(doc)
    for model in _models(doc):
        _extensions_to_internal(model)
        for ds in _list(model.get("datasets")):
            _extensions_to_internal(ds)
            for field in _list(ds.get("fields")):
                _extensions_to_internal(field)
                _expression_to_internal(field)
            for metric in _list(ds.get("metrics")):
                _extensions_to_internal(metric)
                _expression_to_internal(metric)
        for rel in _list(model.get("relationships")):
            _extensions_to_internal(rel)
        for metric in _list(model.get("metrics")):
            _extensions_to_internal(metric)
            _expression_to_internal(metric)
        _lift_dataset_metrics(model, warn)
    return doc


def to_v03(doc: dict[str, Any], warn: Callable[[str], None]) -> dict[str, Any]:
    """Rewrite the export's ``custom_extensions`` as ``nonportable_extensions``, in place."""
    for owner in _extension_owners(doc):
        entries = owner.pop("custom_extensions", None)
        if not entries:
            continue
        converted = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            vendor = entry.get("vendor_name")
            converted.append(
                {"vendor_name": vendor, "data": _as_object(entry.get("data"), vendor, warn)}
            )
        owner["nonportable_extensions"] = converted
    return doc


def _models(doc: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(doc.get("semantic_model"), list):
        return [m for m in doc["semantic_model"] if isinstance(m, dict)]
    return [doc] if "datasets" in doc else []


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _extension_owners(doc: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield doc
    for ds in _list(doc.get("datasets")):
        yield ds
        yield from _list(ds.get("fields"))
        yield from _list(ds.get("metrics"))
    yield from _list(doc.get("relationships"))
    yield from _list(doc.get("metrics"))


def _extensions_to_internal(owner: dict[str, Any]) -> None:
    """``nonportable_extensions`` (object data) -> ``custom_extensions`` (JSON text)."""
    entries = owner.pop("nonportable_extensions", None)
    if not isinstance(entries, list):
        return
    internal = owner.setdefault("custom_extensions", [])
    for entry in entries:
        if isinstance(entry, dict):
            internal.append(
                {"vendor_name": entry.get("vendor_name"), "data": json.dumps(entry.get("data", {}))}
            )


def _as_object(data: Any, vendor: Any, warn: Callable[[str], None]) -> dict[str, Any]:
    """Extension data as the JSON object ``0.3.0.dev0`` requires."""
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            parsed = data
        if isinstance(parsed, dict):
            return parsed
        data = parsed
    warn(
        f"Vendor '{vendor}' extension data is not a JSON object, which Ossie 0.3.0.dev0 "
        f'requires; exported as {{"value": ...}}.'
    )
    return {"value": data}


def _expression_to_internal(owner: dict[str, Any]) -> None:
    """``expression.sql`` -> the ``OSSIE_SQL_2026`` entry of ``expression.dialects``."""
    expression = owner.get("expression")
    if not isinstance(expression, dict) or "sql" not in expression:
        return
    sql = expression.pop("sql")
    dialects = [
        d
        for d in _list(expression.get("dialects"))
        if d.get("dialect") != _OSSIE_SQL  # ``sql`` is authoritative over it
    ]
    if isinstance(sql, str) and sql:
        dialects.insert(0, {"dialect": _OSSIE_SQL, "expression": sql})
    expression["dialects"] = dialects


def _lift_dataset_metrics(model: dict[str, Any], warn: Callable[[str], None]) -> None:
    """Move each dataset's metrics to the model, their references qualified.

    A dataset-scoped metric means the same as a model-scoped one over that
    dataset alone: ``SUM(amount)`` on ``orders`` is ``SUM(orders.amount)``, and
    ``COUNT(*)`` counts the dataset's rows, ``COUNT(orders.<primary key>)``.
    OBML has one namespace for measures, so a name another metric already takes
    becomes ``"<dataset> <metric>"``, with a warning. Each lifted metric keeps
    where it was declared (:data:`SCOPE_KEY`), so one the import cannot convert
    is written back on its dataset.
    """
    own = _list(model.get("metrics"))
    # Lifted first: a model-scoped metric over ``AGG(dataset.metric)`` reuses the
    # measure its dataset metric became, which has to exist by then.
    model_metrics: list[dict[str, Any]] = []
    taken = {m.get("name") for m in own}
    # ``AGG(dataset.metric)`` targets, by lower-cased (dataset, metric): each
    # SQL dialect's qualified expression.
    targets: dict[tuple[str, str], dict[str, str]] = {}
    for ds in _list(model.get("datasets")):
        scoped = ds.pop("metrics", None)
        if not isinstance(scoped, list):
            continue
        ds_name = str(ds.get("name", ""))
        declared = ds.get("primary_key")
        pk: list[Any] = declared if isinstance(declared, list) else []
        sources = _SourceColumns(ds, warn)
        for metric in _list(scoped):
            name = str(metric.get("name", ""))
            lifted = dict(metric)
            lifted[SCOPE_KEY] = {"dataset": ds_name, "metric": copy.deepcopy(metric)}
            lifted["name"] = _unique_name(name, ds_name, taken)
            if lifted["name"] != name:
                warn(
                    f"Dataset-scoped metric '{ds_name}.{name}' is imported as "
                    f"'{lifted['name']}': the model has another metric named '{name}'."
                )
            taken.add(lifted["name"])
            expression = lifted.get("expression")
            if isinstance(expression, dict):
                dialects = [
                    _qualified(d, ds_name, pk, name, sources, warn)
                    for d in _list(expression.get("dialects"))
                ]
                lifted["expression"] = {**expression, "dialects": dialects}
                targets[(ds_name.lower(), name.lower())] = {
                    str(d.get("dialect")): str(d.get("expression"))
                    for d in dialects
                    if d.get("dialect") in _OSI_DIALECT_TO_SQLGLOT
                }
            model_metrics.append(lifted)
    if targets:
        for metric in own:
            expression = metric.get("expression")
            if isinstance(expression, dict):
                expression["dialects"] = [
                    _agg_inlined(d, targets) for d in _list(expression.get("dialects"))
                ]
    if model_metrics:
        model["metrics"] = model_metrics + own


def _unique_name(name: str, ds_name: str, taken: set[Any]) -> str:
    """*name*, or ``"<dataset> <name>"`` (numbered on) when another metric takes it."""
    if name not in taken:
        return name
    candidate = f"{ds_name} {name}"
    number = 2
    while candidate in taken:
        candidate = f"{ds_name} {name} {number}"
        number += 1
    return candidate


class _SourceColumns:
    """The field of a dataset that computes a column of its source, as is.

    ``source.amount`` names the physical column, not the field called
    ``amount``, which may compute something else (``discounted_amount``). It
    resolves to a field whose expression is exactly that column; where none
    is, a field is added for it, since OBML reads only declared columns.
    """

    def __init__(self, ds: dict[str, Any], warn: Callable[[str], None]) -> None:
        self._ds = ds
        self._warn = warn
        # Compared as references resolve: a bare name regardless of case, so
        # ``source_amount`` beside ``SOURCE_AMOUNT`` made both ambiguous.
        self._taken = {_identifier_key(str(f.get("name"))) for f in _list(ds.get("fields"))}

    def field_for(self, column: exp.Identifier, read: str | None, metric: str) -> str:
        wanted = _identifier_key(column.sql(dialect=read))
        for field in _list(self._ds.get("fields")):
            if _identifier_key(_field_sql(field)) == wanted:
                return str(field.get("name"))
        stem = "source_" + re.sub(r"\W+", "_", column.name)
        name, number = stem, 2
        while _identifier_key(name) in self._taken:
            name = f"{stem}_{number}"
            number += 1
        self._taken.add(_identifier_key(name))
        self._ds.setdefault("fields", []).append(
            {
                "name": name,
                "expression": {
                    "dialects": [{"dialect": _OSSIE_SQL, "expression": column.sql(dialect=read)}]
                },
            }
        )
        self._warn(
            f"Dataset-scoped metric '{self._ds.get('name')}.{metric}' reads source column "
            f"'{column.name}', which no field computes as is; added field '{name}' for it."
        )
        return name


def _field_sql(field: dict[str, Any]) -> str:
    """A field's SQL expression in the internal shape (portable form first)."""
    expression = field.get("expression")
    if isinstance(expression, dict):
        for d in _list(expression.get("dialects")):
            if d.get("dialect") in _OSI_DIALECT_TO_SQLGLOT and isinstance(d.get("expression"), str):
                return str(d["expression"])
    return ""


def _identifier_key(text: str) -> str:
    """An identifier compared as SQL compares it: quoted exactly, bare by case."""
    text = text.strip()
    if len(text) > 1 and text[0] == text[-1] == '"':
        return text[1:-1]
    return text.upper()


def _agg_inlined(
    dialect: dict[str, Any], targets: dict[tuple[str, str], dict[str, str]]
) -> dict[str, Any]:
    """Replace each ``AGG(dataset.metric)`` with that metric's expression.

    Spec 0.3 lets a model-scoped metric evaluate a dataset-scoped one only this
    way. Inlined, the aggregate is decomposed like any other, and the import
    points the formula at the measure the dataset metric already became (equal
    definitions are merged). A reference to no dataset-scoped metric is left as
    written, and the import preserves the metric verbatim.
    """
    tag = str(dialect.get("dialect", ""))
    text = dialect.get("expression")
    if tag not in _OSI_DIALECT_TO_SQLGLOT or not isinstance(text, str) or "agg" not in text.lower():
        return dialect
    read = _OSI_DIALECT_TO_SQLGLOT[tag]
    try:
        tree = parse_sql(text, read)
    except Exception:  # noqa: BLE001 - unparseable text is kept; the import preserves it
        return dialect
    changed = False
    for call in list(tree.walk()):
        ref = _agg_argument(call)
        if not isinstance(ref, exp.Column) or not ref.table:
            continue
        forms = targets.get((ref.table.lower(), ref.name.lower()))
        body = forms and (forms.get(tag) or next(iter(forms.values()), None))
        if not body:
            continue
        try:
            inlined = parse_sql(body, read)
        except Exception:  # noqa: BLE001 - leave the call; the import preserves the metric
            continue
        replacement = inlined if isinstance(inlined, exp.Func) else exp.Paren(this=inlined)
        if call is tree:  # the whole expression is the call: replace() has no parent to edit
            tree = replacement
        else:
            call.replace(replacement)
        changed = True
    return {**dialect, "expression": tree.sql(dialect=read)} if changed else dialect


# sqlglot 30 parses ``AGG(x)`` as its own node; other versions as an anonymous call.
_AGG_NODE: type[exp.Expr] | None = getattr(exp, "Agg", None)


def _agg_argument(node: exp.Expr) -> exp.Expr | None:
    """The single argument of an ``AGG(...)`` call, else ``None``."""
    if _AGG_NODE is not None and isinstance(node, _AGG_NODE):
        return node.this if not node.expressions else None
    if isinstance(node, exp.Anonymous) and str(node.this).upper() == "AGG":
        args = node.expressions or []
        return args[0] if len(args) == 1 else None
    return None


def _qualified(
    dialect: dict[str, Any],
    ds_name: str,
    pk: list[Any],
    metric: str,
    sources: _SourceColumns,
    warn: Callable[[str], None],
) -> dict[str, Any]:
    """One dialect entry of a dataset-scoped metric, written as a model-scoped one.

    An expression in a language sqlglot does not read (DAX, MDX, ...) is kept as
    written; the import preserves such a metric verbatim anyway.
    """
    tag = str(dialect.get("dialect", ""))
    text = dialect.get("expression")
    if tag not in _OSI_DIALECT_TO_SQLGLOT or not isinstance(text, str):
        return dialect
    read = _OSI_DIALECT_TO_SQLGLOT[tag]
    try:
        tree = parse_sql(text, read)
    except Exception:  # noqa: BLE001 - unparseable text is kept; the import preserves it
        return dialect
    table = exp.to_identifier(ds_name)
    for column in list(tree.find_all(exp.Column)):
        qualifier = column.table
        if qualifier and qualifier.lower() == _SOURCE_QUALIFIER:
            field = sources.field_for(column.this, read, metric)
            column.set("this", exp.to_identifier(field))
            column.set("table", table.copy())
        elif not qualifier:
            column.set("table", table.copy())
    for count in list(tree.find_all(exp.Count)):
        if not isinstance(count.this, exp.Star):
            continue
        if len(pk) == 1 and isinstance(pk[0], str):
            count.set("this", exp.column(exp.to_identifier(pk[0]), table=table.copy()))
        else:
            warn(
                f"Dataset-scoped metric '{ds_name}.{metric}' counts rows with COUNT(*), "
                f"and '{ds_name}' has no single-column primary_key to count instead; "
                f"query the data object's own row count, '{ds_name} Count'."
            )
    return {**dialect, "expression": tree.sql(dialect=read)}

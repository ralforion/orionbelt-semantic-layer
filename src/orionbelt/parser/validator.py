"""Semantic validation: cycles, ambiguous joins, reference integrity (spec §3.8)."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator

import networkx as nx

from orionbelt.models.errors import SemanticError
from orionbelt.models.expressions import (
    QUALIFIED_COLUMN_REF,
    find_function_calls,
    find_placeholders,
    find_qualified_refs,
)
from orionbelt.models.functions import JSON_PATH_RE, TIME_UNITS, lookup_function
from orionbelt.models.semantic import (
    DataColumnRef,
    DataType,
    ExpressionMode,
    Measure,
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    SemanticModel,
)
from orionbelt.models.synthesis import count_label, model_count_pattern
from orionbelt.models.warnings import WarningCode


class SemanticValidator:
    """Validates semantic rules from spec §3.8."""

    def validate(self, model: SemanticModel) -> list[SemanticError]:
        errors: list[SemanticError] = []
        errors.extend(self._check_unique_identifiers(model))
        errors.extend(self._check_unique_column_names(model))
        errors.extend(self._check_secondary_joins(model))
        errors.extend(self._check_no_cyclic_joins(model))
        errors.extend(self._check_no_multipath_joins(model))
        errors.extend(self._check_measures_resolve(model))
        errors.extend(self._check_join_targets_exist(model))
        errors.extend(self._check_references_resolve(model))
        errors.extend(self._check_num_class_on_numeric_columns(model))
        errors.extend(self._check_time_grain_on_temporal_columns(model))
        errors.extend(self._check_measure_filter_refs(model))
        errors.extend(self._check_within_group_refs(model))
        errors.extend(self._check_computed_column_refs(model))
        errors.extend(self._check_expression_functions(model))
        errors.extend(self._check_query_timezone_coverage(model))
        errors.extend(self._check_reference_name_collisions(model))
        errors.extend(self._check_no_cyclic_computed_columns(model))
        errors.extend(self._check_join_key_expressions(model))
        errors.extend(self._check_distinct_within_group(model))
        errors.extend(self._check_via_reachability(model))
        errors.extend(self._check_missing_via(model))
        errors.extend(self._check_measure_anchors(model))
        errors.extend(self._check_nested_objects(model))
        return errors

    def _check_nested_objects(self, model: SemanticModel) -> list[SemanticError]:
        """Rules a ``nestedIn`` object has to satisfy.

        A nested object's rows exist only inside its parent's, which constrains
        it in ways an ordinary object is not:

        * its parent has to exist, and cannot be itself;
        * the chain of parents has to terminate, or a leg would never reach a
          table to select from;
        * nothing may join **to** it. There is no key to join on - the parent
          correlation is containment rather than an equality - and its rows
          cannot be addressed from outside the parent. Emitting SQL for such a
          join is not possible, so it is refused here rather than later.

        Joining *from* a nested object to a third one is fine and deliberately
        not checked: the nested object is already in FROM through its parent,
        and the join it declares is an ordinary keyed one.
        """
        errors: list[SemanticError] = []
        nested = {name: obj for name, obj in model.data_objects.items() if obj.is_nested}

        for name, obj in nested.items():
            assert obj.nested_in is not None
            parent = obj.nested_in.data_object
            path = f"dataObjects.{name}.nestedIn"
            if parent == name:
                errors.append(
                    SemanticError(
                        code="INVALID_NESTED_SOURCE",
                        message=f"Data object '{name}' is nested in itself.",
                        path=path,
                    )
                )
                continue
            if parent not in model.data_objects:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_DATA_OBJECT",
                        message=(
                            f"Data object '{name}' is nested in '{parent}', which is not "
                            f"a data object in this model."
                        ),
                        path=path,
                    )
                )
                continue
            # Walk to a non-nested ancestor. An array inside an array is
            # supported, so depth is fine; a cycle is not.
            seen = {name}
            cursor = parent
            while cursor in nested:
                if cursor in seen:
                    errors.append(
                        SemanticError(
                            code="INVALID_NESTED_SOURCE",
                            message=(
                                f"Data object '{name}' has a cyclic nestedIn chain through "
                                f"'{cursor}', so it never reaches a table."
                            ),
                            path=path,
                        )
                    )
                    break
                seen.add(cursor)
                next_parent = nested[cursor].nested_in
                assert next_parent is not None
                cursor = next_parent.data_object

        for name, obj in model.data_objects.items():
            for idx, join in enumerate(obj.joins):
                target = nested.get(join.join_to)
                if target is not None and target.nested_in is not None:
                    errors.append(
                        SemanticError(
                            code="INVALID_NESTED_SOURCE",
                            message=(
                                f"Data object '{name}' joins to '{join.join_to}', which is "
                                f"nested in '{target.nested_in.data_object}'. "
                                f"A nested object has no key to join on - its rows exist "
                                f"only inside its parent's - so it can only be reached "
                                f"through that parent."
                            ),
                            path=f"dataObjects.{name}.joins[{idx}].joinTo",
                        )
                    )
        return errors

    def _check_unique_identifiers(self, model: SemanticModel) -> list[SemanticError]:
        """Ensure no duplicate names across dimensions, measures, and metrics.

        Data object names live in a separate namespace — a dimension may share
        its name with a data object (e.g. dimension "Region" on data object "Region").
        """
        errors: list[SemanticError] = []
        all_names: dict[str, str] = {}  # name -> type

        def _register(name: str, kind: str, path: str) -> None:
            existing = all_names.get(name)
            if existing is not None:
                errors.append(
                    SemanticError(
                        code="DUPLICATE_IDENTIFIER",
                        message=(
                            f"{kind.title()} '{name}' conflicts with existing {existing} '{name}'"
                        ),
                        path=path,
                    )
                )
            all_names[name] = kind

        for name in model.dimensions:
            _register(name, "dimension", f"dimensions.{name}")

        for name in model.measures:
            _register(name, "measure", f"measures.{name}")

        for name in model.metrics:
            _register(name, "metric", f"metrics.{name}")

        # Synthesized count measures (name == resolved count label, e.g.
        # "Sales Count") occupy the measure namespace too (models/synthesis.py).
        # A declared measure of the same name is the intended override (D4) and
        # is fine; but a dimension or metric with that name would be shadowed by
        # the synthesized measure at query time, so reject the collision. Two
        # countable objects that resolve to the same count name also collide.
        if getattr(model, "expose_counts", True):
            pattern = model_count_pattern(model)
            seen_counts: dict[str, str] = {}  # count name -> data object key
            for obj_key, obj in model.data_objects.items():
                if not obj.countable:
                    continue
                cid = count_label(obj_key, obj, pattern)
                clashing = all_names.get(cid)
                if clashing in ("dimension", "metric"):
                    errors.append(
                        SemanticError(
                            code="DUPLICATE_IDENTIFIER",
                            message=(
                                f"{str(clashing).title()} '{cid}' conflicts with the synthesized "
                                f"count measure for data object '{obj_key}'. Rename it, set "
                                f"'countLabel'/'countLabelPattern', or 'countable: false'."
                            ),
                            path=f"{clashing}s.{cid}",
                        )
                    )
                elif cid in seen_counts:
                    errors.append(
                        SemanticError(
                            code="DUPLICATE_IDENTIFIER",
                            message=(
                                f"Data objects '{seen_counts[cid]}' and '{obj_key}' both "
                                f"synthesize a count measure named '{cid}'. Give one a distinct "
                                f"'countLabel' or set 'countable: false'."
                            ),
                            path=f"dataObjects.{obj_key}.countLabel",
                        )
                    )
                else:
                    seen_counts[cid] = obj_key

        return errors

    def _check_unique_column_names(self, model: SemanticModel) -> list[SemanticError]:
        """Column names must be unique within each data object.

        Duplicate YAML keys are now rejected at parse time by TrackedLoader
        (``allow_duplicate_keys = False``). This validator is retained as a
        structural hook in case models are constructed programmatically.
        """
        return []

    def _check_secondary_joins(self, model: SemanticModel) -> list[SemanticError]:
        """Validate secondary join constraints.

        - Every secondary join MUST have a pathName.
        - pathName must be unique per (source, target) pair.
        """
        errors: list[SemanticError] = []
        # Track pathName per (source, target) pair
        path_names: dict[tuple[str, str], set[str]] = {}

        for obj_name, obj in model.data_objects.items():
            for i, join in enumerate(obj.joins):
                if join.secondary and not join.path_name:
                    errors.append(
                        SemanticError(
                            code="SECONDARY_JOIN_MISSING_PATH_NAME",
                            message=(
                                f"Data object '{obj_name}' join[{i}] is secondary "
                                f"but has no pathName"
                            ),
                            path=f"dataObjects.{obj_name}.joins[{i}]",
                        )
                    )
                if join.path_name:
                    pair = (obj_name, join.join_to)
                    if pair not in path_names:
                        path_names[pair] = set()
                    if join.path_name in path_names[pair]:
                        errors.append(
                            SemanticError(
                                code="DUPLICATE_JOIN_PATH_NAME",
                                message=(
                                    f"Data object '{obj_name}' join[{i}] has duplicate "
                                    f"pathName '{join.path_name}' for target '{join.join_to}'"
                                ),
                                path=f"dataObjects.{obj_name}.joins[{i}]",
                            )
                        )
                    else:
                        path_names[pair].add(join.path_name)

        return errors

    def _check_no_cyclic_joins(self, model: SemanticModel) -> list[SemanticError]:
        """Detect cyclic join paths."""
        errors: list[SemanticError] = []

        # Build adjacency list from joins (skip secondary joins)
        adj: dict[str, set[str]] = {}
        for obj_name, obj in model.data_objects.items():
            if obj_name not in adj:
                adj[obj_name] = set()
            for join in obj.joins:
                if not join.secondary:
                    adj[obj_name].add(join.join_to)

        # Iterative DFS cycle detection (avoids RecursionError on large models)
        visited: set[str] = set()
        rec_stack: set[str] = set()

        for start in adj:
            if start in visited:
                continue
            stack: list[tuple[str, list[str]]] = [(start, iter(adj.get(start, set())))]  # type: ignore[list-item]
            path: list[str] = [start]
            visited.add(start)
            rec_stack.add(start)

            while stack:
                node, neighbors = stack[-1]
                advanced = False
                for neighbor in neighbors:
                    if neighbor not in visited:
                        visited.add(neighbor)
                        rec_stack.add(neighbor)
                        path.append(neighbor)
                        stack.append((neighbor, iter(adj.get(neighbor, set()))))  # type: ignore[arg-type]
                        advanced = True
                        break
                    elif neighbor in rec_stack:
                        if neighbor in path:
                            cycle = path[path.index(neighbor) :] + [neighbor]
                        else:
                            cycle = [node, neighbor]
                        errors.append(
                            SemanticError(
                                code="CYCLIC_JOIN",
                                message=f"Cyclic join detected: {' -> '.join(cycle)}",
                                path=f"dataObjects.{node}.joins",
                            )
                        )
                if not advanced:
                    stack.pop()
                    rec_stack.discard(node)
                    if path:
                        path.pop()

        return errors

    def _check_no_multipath_joins(self, model: SemanticModel) -> list[SemanticError]:
        """Detect multiple distinct paths between any pair of nodes in the join DAG.

        Only flags true diamonds where both paths go through intermediaries.
        A direct edge from start to target is canonical, so an additional
        indirect path (e.g. Purchases→Suppliers direct + Purchases→Products→Suppliers)
        is not ambiguous and is not flagged.
        """
        errors: list[SemanticError] = []

        # Build adjacency list from joins (skip secondary joins)
        adj: dict[str, list[str]] = {}
        for obj_name, obj in model.data_objects.items():
            if obj_name not in adj:
                adj[obj_name] = []
            for join in obj.joins:
                if not join.secondary:
                    adj[obj_name].append(join.join_to)

        reported: set[tuple[str, str]] = set()

        for start in adj:
            if not adj[start]:
                continue
            # BFS from start; track first parent that reached each node
            direct_neighbors: set[str] = set()
            first_parent: dict[str, str] = {}
            queue: deque[tuple[str, str]] = deque()
            for neighbor in adj[start]:
                if neighbor == start:
                    continue
                direct_neighbors.add(neighbor)
                if neighbor not in first_parent:
                    first_parent[neighbor] = start
                    queue.append((neighbor, start))

            while queue:
                node, _parent = queue.popleft()
                for neighbor in adj.get(node, []):
                    if neighbor == start:
                        continue
                    if neighbor not in first_parent:
                        first_parent[neighbor] = node
                        queue.append((neighbor, node))
                    elif first_parent[neighbor] != node:
                        # Skip if target has a direct edge from start —
                        # the direct join is the canonical path.
                        if neighbor in direct_neighbors:
                            continue
                        pair = (start, neighbor)
                        if pair not in reported:
                            reported.add(pair)
                            errors.append(
                                SemanticError(
                                    code="MULTIPATH_JOIN",
                                    message=(
                                        f"Multiple join paths from '{start}' to "
                                        f"'{neighbor}' (via '{first_parent[neighbor]}' "
                                        f"and '{node}'). "
                                        f"Join paths must be unambiguous."
                                    ),
                                    path=f"dataObjects.{start}.joins",
                                )
                            )

        return errors

    def _check_column_ref(
        self,
        ref: DataColumnRef,
        model: SemanticModel,
        *,
        subject: str,
        path: str,
    ) -> list[SemanticError]:
        """Validate one ``DataColumnRef``: both halves present, and both resolving.

        The JSON schema makes ``dataObject`` and ``column`` required, but the
        Pydantic type leaves both optional so models can be built in Python,
        and ``ModelStore.load_model`` does not run the schema. A missing half
        is not inert: codegen renders it as an empty identifier, so a ref
        without a column compiles to ``ORDER BY "Sales".""`` and one without a
        data object to ``ORDER BY ""."Product Name"``.
        """
        obj_name, col_name = ref.view, ref.column

        missing = [
            field for field, value in (("dataObject", obj_name), ("column", col_name)) if not value
        ]
        if missing:
            return [
                SemanticError(
                    code="INCOMPLETE_COLUMN_REF",
                    message=(
                        f"{subject} is missing {' and '.join(missing)}. A column "
                        f"reference needs both dataObject and column; an omitted "
                        f"one compiles to an empty SQL identifier."
                    ),
                    path=path,
                )
            ]

        if obj_name not in model.data_objects:
            return [
                SemanticError(
                    code="UNKNOWN_DATA_OBJECT",
                    message=f"{subject} references unknown data object '{obj_name}'",
                    path=path,
                )
            ]

        if col_name not in model.data_objects[obj_name].columns:
            return [
                SemanticError(
                    code="UNKNOWN_COLUMN",
                    message=(
                        f"{subject} references unknown column '{col_name}' "
                        f"in data object '{obj_name}'"
                    ),
                    path=path,
                )
            ]

        return []

    def _check_measures_resolve(self, model: SemanticModel) -> list[SemanticError]:
        """Ensure measure column references resolve to actual data object columns."""
        errors: list[SemanticError] = []
        for name, measure in model.measures.items():
            for i, col_ref in enumerate(measure.columns):
                errors.extend(
                    self._check_column_ref(
                        col_ref,
                        model,
                        subject=f"Measure '{name}' column[{i}]",
                        path=f"measures.{name}.columns[{i}]",
                    )
                )
        return errors

    def _check_join_targets_exist(self, model: SemanticModel) -> list[SemanticError]:
        """Ensure join targets reference existing data objects."""
        errors: list[SemanticError] = []
        for obj_name, obj in model.data_objects.items():
            for i, join in enumerate(obj.joins):
                if not join.columns_from or not join.columns_to:
                    errors.append(
                        SemanticError(
                            code="EMPTY_JOIN_COLUMNS",
                            message=(
                                f"Data object '{obj_name}' join[{i}] to "
                                f"'{join.join_to}' has empty join columns"
                            ),
                            path=f"dataObjects.{obj_name}.joins[{i}]",
                        )
                    )
                elif len(join.columns_from) != len(join.columns_to):
                    errors.append(
                        SemanticError(
                            code="JOIN_COLUMN_COUNT_MISMATCH",
                            message=(
                                f"Data object '{obj_name}' join[{i}] has "
                                f"{len(join.columns_from)} columnsFrom and "
                                f"{len(join.columns_to)} columnsTo"
                            ),
                            path=f"dataObjects.{obj_name}.joins[{i}]",
                        )
                    )
                if join.join_to not in model.data_objects:
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_JOIN_TARGET",
                            message=(
                                f"Data object '{obj_name}' join[{i}] references "
                                f"unknown data object '{join.join_to}'"
                            ),
                            path=f"dataObjects.{obj_name}.joins[{i}]",
                        )
                    )
                else:
                    # Validate join columns exist
                    for col_name in join.columns_from:
                        if col_name not in obj.columns:
                            errors.append(
                                SemanticError(
                                    code="UNKNOWN_JOIN_COLUMN",
                                    message=(
                                        f"Data object '{obj_name}' join[{i}] columnsFrom "
                                        f"references unknown column '{col_name}'"
                                    ),
                                    path=f"dataObjects.{obj_name}.joins[{i}].columnsFrom",
                                )
                            )
                    target_obj = model.data_objects[join.join_to]
                    for col_name in join.columns_to:
                        if col_name not in target_obj.columns:
                            errors.append(
                                SemanticError(
                                    code="UNKNOWN_JOIN_COLUMN",
                                    message=(
                                        f"Data object '{obj_name}' join[{i}] columnsTo "
                                        f"references unknown column '{col_name}' "
                                        f"in data object '{join.join_to}'"
                                    ),
                                    path=f"dataObjects.{obj_name}.joins[{i}].columnsTo",
                                )
                            )
        return errors

    def _check_references_resolve(self, model: SemanticModel) -> list[SemanticError]:
        """Ensure dimension references resolve."""
        errors: list[SemanticError] = []
        for name, dim in model.dimensions.items():
            errors.extend(
                self._check_column_ref(
                    DataColumnRef(view=dim.view, column=dim.column),
                    model,
                    subject=f"Dimension '{name}'",
                    path=f"dimensions.{name}",
                )
            )
        return errors

    _NUMERIC_TYPES = {DataType.INT, DataType.FLOAT}
    _TIME_GRAIN_TYPES = {DataType.DATE, DataType.TIMESTAMP, DataType.TIMESTAMP_TZ}

    def _check_time_grain_on_temporal_columns(self, model: SemanticModel) -> list[SemanticError]:
        """Ensure timeGrain is only set when the underlying column is temporal.

        ``timeGrain`` compiles to ``date_trunc(grain, column)``, which fails at
        runtime if the column's abstractType is not date/timestamp/timestamp_tz.
        Reject at model-load time so the error surfaces during validation rather
        than during the first query.
        """
        errors: list[SemanticError] = []
        for name, dim in model.dimensions.items():
            if dim.time_grain is None:
                continue
            obj_name = dim.view
            col_name = dim.column
            if not obj_name or not col_name:
                continue
            obj = model.data_objects.get(obj_name)
            if obj is None or col_name not in obj.columns:
                # Caught by _check_references_resolve.
                continue
            col = obj.columns[col_name]
            if col.abstract_type not in self._TIME_GRAIN_TYPES:
                errors.append(
                    SemanticError(
                        code="TIME_GRAIN_ON_NON_TEMPORAL",
                        message=(
                            f"Dimension '{name}' has timeGrain "
                            f"'{dim.time_grain.value}' but underlying column "
                            f"'{obj_name}.{col_name}' has abstractType "
                            f"'{col.abstract_type.value}'. timeGrain requires "
                            f"the column to be date, timestamp, or timestamp_tz. "
                            f"Drop timeGrain, fix the column's abstractType, or "
                            f"define a computed column with to_date()."
                        ),
                        path=f"dimensions.{name}",
                    )
                )
        return errors

    def _check_num_class_on_numeric_columns(self, model: SemanticModel) -> list[SemanticError]:
        """Ensure numClass is only set on numeric columns (int or float)."""
        errors: list[SemanticError] = []
        for obj_name, obj in model.data_objects.items():
            for col_name, col in obj.columns.items():
                if col.num_class and col.abstract_type not in self._NUMERIC_TYPES:
                    errors.append(
                        SemanticError(
                            code="NUM_CLASS_ON_NON_NUMERIC",
                            message=(
                                f"Column '{col_name}' in data object '{obj_name}' "
                                f"has numClass '{col.num_class}' but abstractType "
                                f"'{col.abstract_type}' is not numeric (int or float)"
                            ),
                            path=f"dataObjects.{obj_name}.columns.{col_name}",
                        )
                    )
        return errors

    def _check_distinct_within_group(self, model: SemanticModel) -> list[SemanticError]:
        """Reject ``distinct: true`` + a ``withinGroup`` that is not the aggregated column.

        SQL restricts a DISTINCT aggregate's ORDER BY to expressions that appear
        in its argument list: the engine sorts the deduplicated values, so it
        cannot order them by something it has just collapsed away. Postgres,
        DuckDB and BigQuery all reject it outright ("In a DISTINCT aggregate,
        ORDER BY expressions must appear in the argument list").

        Without this check the model loads happily and every query touching the
        measure fails at execution time with a driver-level binder error, which
        points at generated SQL rather than at the two lines of OBML that caused
        it.
        """
        errors: list[SemanticError] = []
        for measure_name, measure in model.measures.items():
            if not measure.distinct or measure.within_group is None:
                continue

            ordered = measure.within_group.column
            ordered_ref = (ordered.view or "", ordered.column or "")
            if ordered_ref in self._aggregated_column_refs(measure):
                continue

            aggregated = self._describe_aggregated_columns(measure)
            errors.append(
                SemanticError(
                    code="WITHIN_GROUP_NOT_IN_DISTINCT_ARGS",
                    message=(
                        f"Measure '{measure_name}' is DISTINCT but orders by "
                        f"'{ordered_ref[0]}.{ordered_ref[1]}', which is not among the "
                        f"columns it aggregates ({aggregated}). A DISTINCT aggregate "
                        f"can only be ordered by an expression in its argument list, "
                        f"so this fails at execution time on Postgres, DuckDB and "
                        f"BigQuery among others."
                    ),
                    path=f"measures.{measure_name}.withinGroup",
                    hint=(
                        "Order by the aggregated column itself, or drop "
                        "`distinct: true` if the ordering matters more than "
                        "deduplication."
                    ),
                )
            )
        return errors

    @staticmethod
    def _aggregated_column_refs(measure: Measure) -> set[tuple[str, str]]:
        """The ``(dataObject, column)`` pairs that form a measure's aggregate argument.

        Only a bare column reference can be matched against a ``withinGroup``
        column. An ``expression`` that computes something (``a || b``) aggregates
        that computed value, not its parts, so ordering by any single part is
        still outside the argument list — hence the empty set.
        """
        if measure.columns:
            return {(c.view or "", c.column or "") for c in measure.columns}
        if measure.expression:
            body = measure.expression.strip()
            refs = find_qualified_refs(body)
            if len(refs) == 1 and QUALIFIED_COLUMN_REF.fullmatch(body) is not None:
                return {refs[0]}
        return set()

    @staticmethod
    def _describe_aggregated_columns(measure: Measure) -> str:
        refs = SemanticValidator._aggregated_column_refs(measure)
        if refs:
            return ", ".join(f"'{obj}.{col}'" for obj, col in sorted(refs))
        return "a computed expression, which cannot be matched by column"

    def _check_measure_filter_refs(self, model: SemanticModel) -> list[SemanticError]:
        """Verify that measure filter columns reference existing data objects and columns."""
        errors: list[SemanticError] = []
        for meas_name, measure in model.measures.items():
            for fi in measure.filters:
                self._validate_filter_item(fi, model, meas_name, errors)
        return errors

    def _validate_filter_item(
        self,
        item: MeasureFilterItem,
        model: SemanticModel,
        meas_name: str,
        errors: list[SemanticError],
    ) -> None:
        """Recursively validate a measure filter item."""
        if isinstance(item, MeasureFilter):
            if item.column is None:
                return
            view, column = item.column.view, item.column.column
            # An omitted half reaches codegen as an empty identifier, the same
            # way it does for a dimension or a withinGroup column.
            missing = [
                field for field, value in (("dataObject", view), ("column", column)) if not value
            ]
            if missing or not view or not column:
                errors.append(
                    SemanticError(
                        code="INCOMPLETE_COLUMN_REF",
                        message=(
                            f"Measure '{meas_name}' filter is missing "
                            f"{' and '.join(missing)}. A column reference needs both "
                            f"dataObject and column; an omitted one compiles to an "
                            f"empty SQL identifier."
                        ),
                        path=f"measures.{meas_name}.filters",
                    )
                )
                return
            obj = model.data_objects.get(view)
            if not obj:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_FILTER_DATA_OBJECT",
                        message=(
                            f"Measure '{meas_name}' filter references unknown data object '{view}'"
                        ),
                        path=f"measures.{meas_name}.filters",
                    )
                )
                return
            if column not in obj.columns:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_FILTER_COLUMN",
                        message=(
                            f"Measure '{meas_name}' filter references unknown "
                            f"column '{column}' in '{view}'"
                        ),
                        path=f"measures.{meas_name}.filters",
                    )
                )
        elif isinstance(item, MeasureFilterGroup):
            for child in item.filters:
                self._validate_filter_item(child, model, meas_name, errors)

    def _check_within_group_refs(self, model: SemanticModel) -> list[SemanticError]:
        """Verify that a ``withinGroup`` ordering column exists.

        ``withinGroup`` is the one ``DataColumnRef`` site on a measure that no
        other check covers: ``columns:`` goes through
        :meth:`_check_measures_resolve` and filter columns through
        :meth:`_check_measure_filter_refs`. Left unchecked, a typo compiles
        straight into ``ORDER BY "Sales"."no_such_col"`` inside the LISTAGG and
        only fails at the database.
        """
        errors: list[SemanticError] = []
        for name, measure in model.measures.items():
            if measure.within_group is None:
                continue
            errors.extend(
                self._check_column_ref(
                    measure.within_group.column,
                    model,
                    subject=f"Measure '{name}' withinGroup",
                    path=f"measures.{name}.withinGroup",
                )
            )
        return errors

    def _check_computed_column_refs(self, model: SemanticModel) -> list[SemanticError]:
        """Ensure a computed column's references name columns that exist.

        Two forms, checked the same way: ``{name}`` must name a sibling column
        of the same data object, and ``{[Data Object].[Column]}`` must name a
        column of a data object the model declares.

        A reference the compiler cannot resolve is not dropped and not
        reported — it survives into codegen as a *string literal*, so
        ``{amount} * {no_such_col}`` emits ``"Sales"."amount" * 'no_such_col'``,
        and a qualified reference to nothing emits the labels as identifiers.
        The model validates clean and the wrongness surfaces, at best, as a
        type error from the database.
        """
        errors: list[SemanticError] = []
        for obj_name, obj in model.data_objects.items():
            for col_name, col in obj.columns.items():
                if not col.expression:
                    continue
                path = f"dataObjects.{obj_name}.columns.{col_name}.expression"
                for ref in find_placeholders(col.expression):
                    if ref in obj.columns:
                        continue
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_COLUMN_IN_EXPRESSION",
                            message=(
                                f"Computed column '{col_name}' in data object "
                                f"'{obj_name}' references unknown column '{ref}'"
                            ),
                            path=path,
                            hint=(
                                "A computed column's {placeholder} must name another "
                                f"column of '{obj_name}'. To reference a column of a "
                                "different data object, use the "
                                "{[Data Object].[Column]} form instead."
                            ),
                        )
                    )
                for ref_object, ref_column in find_qualified_refs(col.expression):
                    target = model.data_objects.get(ref_object)
                    if target is None:
                        errors.append(
                            SemanticError(
                                code="UNKNOWN_DATA_OBJECT_IN_EXPRESSION",
                                message=(
                                    f"Computed column '{col_name}' in data object "
                                    f"'{obj_name}' references unknown data object "
                                    f"'{ref_object}'"
                                ),
                                path=path,
                            )
                        )
                    elif ref_column not in target.columns:
                        errors.append(
                            SemanticError(
                                code="UNKNOWN_COLUMN_IN_EXPRESSION",
                                message=(
                                    f"Computed column '{col_name}' in data object "
                                    f"'{obj_name}' references unknown column "
                                    f"'{ref_column}' in data object '{ref_object}'"
                                ),
                                path=path,
                            )
                        )
        return errors

    @staticmethod
    def _check_query_timezone_coverage(model: SemanticModel) -> list[SemanticError]:
        """Warn when a query time zone cannot reach the model's naive columns.

        ``queryTimezone`` converts timestamp columns so the model, not the
        warehouse session, decides which day or week a row falls in. A column
        that carries no zone cannot be converted until the model says which
        zone it was written in, which is what ``defaultTimezone`` states, and
        the session's own zone is not an answer: it is a fact about the
        connection rather than about the data, and reading it into the SQL
        would make the same query mean different things on different
        connections.

        So those columns are left alone, and this says so rather than leaving
        a model half-converted in silence.
        """
        settings = model.settings
        if settings is None or not settings.query_timezone or settings.default_timezone:
            return []
        naive = [
            f"{obj_name}.{col_name}"
            for obj_name, obj in model.data_objects.items()
            for col_name, col in obj.columns.items()
            if col.abstract_type is DataType.TIMESTAMP
        ]
        if not naive:
            return []
        return [
            SemanticError(
                code=WarningCode.UNDECLARED_TIMESTAMP_ZONE,
                message=(
                    f"settings.queryTimezone is '{settings.query_timezone}', but "
                    f"{len(naive)} timestamp column(s) carry no time zone and "
                    f"settings.defaultTimezone does not say which zone they were "
                    f"written in, so they are read as the warehouse session sees "
                    f"them: {', '.join(sorted(naive)[:5])}"
                ),
                path="settings.queryTimezone",
                hint=(
                    "Set settings.defaultTimezone to the zone those columns are "
                    "stored in, or declare the columns as timestamp_tz if they "
                    "carry one."
                ),
                severity="warning",
                context={"columns": sorted(naive)},
            )
        ]

    @staticmethod
    def _expression_bodies(model: SemanticModel) -> Iterator[tuple[str, str, str]]:
        """``(path, subject, expression)`` for every expression body in *model*.

        The three places an author can write one: a computed column, a measure
        expression, and a metric formula.
        """
        for obj_name, obj in model.data_objects.items():
            for col_name, col in obj.columns.items():
                if col.expression:
                    yield (
                        f"dataObjects.{obj_name}.columns.{col_name}.expression",
                        f"Computed column '{col_name}' in data object '{obj_name}'",
                        col.expression,
                    )
        for measure_name, measure in model.measures.items():
            if measure.expression:
                yield (
                    f"measures.{measure_name}.expression",
                    f"Measure '{measure_name}'",
                    measure.expression,
                )
        for metric_name, metric in model.metrics.items():
            if metric.expression:
                yield (
                    f"metrics.{metric_name}.expression",
                    f"Metric '{metric_name}'",
                    metric.expression,
                )

    def _check_expression_functions(self, model: SemanticModel) -> list[SemanticError]:
        """Reject a portable-catalog function called with the wrong arity.

        Expression bodies used to be pass-through in both directions: any
        ``IDENT(`` became a function call and every dialect emitted it
        verbatim, so ``substring({Zip}, 1, 5, 9)`` validated clean and failed
        at the database — on whichever engine the query happened to run.

        Only functions the catalog defines (``models/functions.py``) are
        checked. Everything else stays the escape hatch it has always been:
        the catalog cannot know a vendor function's arity, and rejecting names
        it does not carry would break every model written before it existed.
        """
        errors: list[SemanticError] = []
        portable = (
            model.settings is not None and model.settings.expression_mode is ExpressionMode.PORTABLE
        )
        for path, subject, expression in self._expression_bodies(model):
            reported: set[str] = set()
            for call in find_function_calls(expression):
                spec = lookup_function(call.name)
                if spec is None:
                    # Outside the catalog: emitted verbatim, so the model runs
                    # only where that function exists. A warning by default, an
                    # error when the model has asked to stay portable. Reported
                    # once per name per expression, since repeating a call is
                    # not a second problem.
                    if call.name.lower() in reported:
                        continue
                    reported.add(call.name.lower())
                    errors.append(
                        SemanticError(
                            code=WarningCode.NON_PORTABLE_FUNCTION,
                            message=(
                                f"{subject} calls '{call.name}', which the portable "
                                f"function catalog does not carry, so it is emitted "
                                f"as written and the model runs only on engines "
                                f"that have it"
                            ),
                            path=path,
                            hint=(
                                "Use a catalog function (GET /v1/reference/functions) "
                                "if one fits, or keep this call and accept the "
                                "dependency. settings.expressionMode: portable turns "
                                "this into an error."
                            ),
                            severity="error" if portable else "warning",
                            context={"function": call.name},
                        )
                    )
                    continue
                if spec.accepts(call.arg_count):
                    continue
                plural = "" if call.arg_count == 1 else "s"
                errors.append(
                    SemanticError(
                        code="WRONG_FUNCTION_ARITY",
                        message=(
                            f"{subject} calls '{call.name}' with {call.arg_count} "
                            f"argument{plural}, but it takes {spec.arity_text}"
                        ),
                        path=path,
                        hint=f"Canonical signature: {spec.signature}.",
                        context={
                            "function": spec.name,
                            "argCount": call.arg_count,
                            "signature": spec.signature,
                        },
                    )
                )
        errors.extend(self._check_expression_units(model))
        errors.extend(self._check_expression_json_paths(model))
        return errors

    @staticmethod
    def _unit_literal(argument: str) -> str | None:
        """The time unit a source argument names, or ``None`` if it names none."""
        text = argument.strip()
        if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
            inner = text[1:-1].lower()
            if inner in TIME_UNITS:
                return inner
        return None

    @staticmethod
    def _json_path_literal(argument: str) -> str | None:
        """The JSONPath a source argument names, or ``None`` if it names none.

        The accepted subset is object member access and array subscripts rooted
        at ``$``. Filters and wildcards are excluded because the engines diverge
        on them and a catalog entry has to pin one meaning.
        """
        text = argument.strip()
        if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
            inner = text[1:-1]
            if JSON_PATH_RE.match(inner):
                return inner
        return None

    def _check_expression_json_paths(self, model: SemanticModel) -> list[SemanticError]:
        """Reject a json call whose path is not a literal the catalog accepts.

        The path cannot be an expression, and for a sharper reason than the
        time unit: the engines take it *apart* differently. Postgres wants the
        segments as separate arguments and Snowflake wants array subscripts
        bracketed, rejecting ``arr.0`` outright. None of that is derivable from
        a runtime value.

        Without this the call still compiles - codegen falls through to the
        pass-through path and emits it verbatim - which is worse than an error:
        it slips past ``expressionMode: portable`` and past a dialect's
        unsupported-function guard, so a model can acquire an engine dependency
        precisely where it asked not to.
        """
        errors: list[SemanticError] = []
        for path, subject, expression in self._expression_bodies(model):
            for call in find_function_calls(expression):
                spec = lookup_function(call.name)
                if spec is None or spec.path_argument is None or not spec.accepts(call.arg_count):
                    continue
                argument = call.arguments[spec.path_argument]
                if self._json_path_literal(argument) is not None:
                    continue
                errors.append(
                    SemanticError(
                        code="INVALID_JSON_PATH",
                        message=(
                            f"{subject} calls '{call.name}' with path {argument}, "
                            f"which is not a literal JSONPath"
                        ),
                        path=path,
                        hint=(
                            "The path is a quoted literal such as '$.a', '$.a.b' or "
                            "'$.a[0]': the dialects take it apart differently, so it "
                            "cannot come from an expression. Filters and wildcards "
                            "are not supported."
                        ),
                        context={"function": spec.name, "path": argument},
                    )
                )
        return errors

    def _check_expression_units(self, model: SemanticModel) -> list[SemanticError]:
        """Reject a date/time call whose unit is not one of the catalog's.

        The unit cannot be an expression, and not for want of trying: every
        dialect switches on it to render the call at all, as a keyword on
        BigQuery and ClickHouse, a quoted string on Snowflake, an interval
        qualifier on MySQL, and a different expression per unit on Postgres.
        A misspelling is caught here rather than compiling to a call the
        engine rejects, or worse, silently accepts as something else.
        """
        errors: list[SemanticError] = []
        for path, subject, expression in self._expression_bodies(model):
            for call in find_function_calls(expression):
                spec = lookup_function(call.name)
                if spec is None or spec.unit_argument is None or not spec.accepts(call.arg_count):
                    continue
                argument = call.arguments[spec.unit_argument]
                if self._unit_literal(argument) is not None:
                    continue
                errors.append(
                    SemanticError(
                        code="UNKNOWN_TIME_UNIT",
                        message=(
                            f"{subject} calls '{call.name}' with unit {argument}, "
                            f"which is not one of {', '.join(TIME_UNITS)}"
                        ),
                        path=path,
                        hint=(
                            "The unit is a quoted literal, not an expression: every "
                            "dialect renders the call differently per unit."
                        ),
                        context={
                            "function": spec.name,
                            "unit": argument,
                            "units": list(TIME_UNITS),
                        },
                    )
                )
        return errors

    def _check_reference_name_collisions(self, model: SemanticModel) -> list[SemanticError]:
        """Refuse an expression reference that two names answer to.

        ``{[Data Object].[Column]}`` is read with the brackets' padding
        stripped, so a reference to ``[ Zip 5 ]`` addresses ``Zip 5``. Where a
        model holds both ``Zip 5`` and ``" Zip 5 "`` the reference names them
        both, and silently binding to one is how an expression comes to read a
        different column than the author wrote.

        Only references are refused, not the names themselves: both columns are
        still addressable by the exact ``dataObject``/``column`` pair a
        dimension or measure uses. It is the bracket syntax that cannot tell
        them apart.
        """
        errors: list[SemanticError] = []

        def collisions(names: list[str], wanted: str) -> list[str]:
            return sorted(name for name in names if name.strip() == wanted)

        def check(refs: list[tuple[str, str]], path: str, subject: str) -> None:
            for ref_object, ref_column in refs:
                matches = collisions(list(model.data_objects), ref_object)
                if len(matches) > 1:
                    errors.append(
                        SemanticError(
                            code="AMBIGUOUS_NAME",
                            message=(
                                f"{subject} references data object '{ref_object}', which "
                                f"{len(matches)} names answer to "
                                f"({', '.join(repr(m) for m in matches)}) — they differ "
                                f"only in surrounding whitespace"
                            ),
                            path=path,
                            hint=(
                                "Bracket references are read with the padding stripped, so "
                                "rename one of them to something a reference can single out."
                            ),
                        )
                    )
                    continue
                target = model.data_objects.get(ref_object)
                if target is None:
                    continue
                column_matches = collisions(list(target.columns), ref_column)
                if len(column_matches) > 1:
                    errors.append(
                        SemanticError(
                            code="AMBIGUOUS_NAME",
                            message=(
                                f"{subject} references column '{ref_column}' on "
                                f"'{ref_object}', which {len(column_matches)} names answer to "
                                f"({', '.join(repr(m) for m in column_matches)}) — they differ "
                                f"only in surrounding whitespace"
                            ),
                            path=path,
                            hint=(
                                "Bracket references are read with the padding stripped, so "
                                "rename one of them to something a reference can single out."
                            ),
                        )
                    )

        for obj_name, obj in model.data_objects.items():
            for col_name, col in obj.columns.items():
                if col.expression:
                    check(
                        find_qualified_refs(col.expression),
                        f"dataObjects.{obj_name}.columns.{col_name}.expression",
                        f"Computed column '{col_name}' in data object '{obj_name}'",
                    )
        for measure_name, measure in model.measures.items():
            if measure.expression:
                check(
                    find_qualified_refs(measure.expression),
                    f"measures.{measure_name}.expression",
                    f"Measure '{measure_name}'",
                )
        return errors

    def _check_no_cyclic_computed_columns(self, model: SemanticModel) -> list[SemanticError]:
        """Detect computed columns whose expressions reference each other in a loop.

        The compiler raises ``RecursionError`` on such a chain, but
        ``_build_computed_column_expr`` catches every exception and falls back
        to a plain reference to the column's own ``code`` — which for a computed
        column is empty, so the *label* is emitted as a physical column name.
        A cycle therefore compiles to SQL naming a column that does not exist.

        Model-wide rather than per data object: a qualified reference lets a
        cycle leave and re-enter an object, and the compiler's cycle guard is
        keyed on ``(object, column)`` for exactly that reason.
        """
        errors: list[SemanticError] = []
        g = self._computed_column_graph(model)
        # Each strongly connected component that is not a single acyclic
        # node is exactly one reference cycle. Using SCCs rather than
        # walking from every column keeps this linear and reports each
        # cycle once, however many columns sit on it.
        for scc in nx.strongly_connected_components(g):
            if len(scc) == 1:
                only = next(iter(scc))
                if not g.has_edge(only, only):
                    continue
            cycle = self._describe_cycle(g, scc)
            obj_name, _, col_name = cycle[0].partition(".")
            errors.append(
                SemanticError(
                    code="CYCLIC_COMPUTED_COLUMN",
                    message=(f"Cyclic computed-column reference: {' -> '.join(cycle)}"),
                    path=f"dataObjects.{obj_name}.columns.{col_name}.expression",
                )
            )
        return errors

    def _check_join_key_expressions(self, model: SemanticModel) -> list[SemanticError]:
        """Refuse a join key whose expression reads another data object.

        A computed column is legal as a join key — ``build_join_condition``
        inlines it — but only while it reads its own object. Reading another
        one puts that object's alias in the ON clause of the join that would
        introduce it: unbound at best, and circular whenever the reference is
        reachable only *through* this join. Nothing downstream can repair that,
        so it is rejected here rather than compiled into SQL the database
        rejects (or, worse, a plan that silently drops the reference).
        """
        errors: list[SemanticError] = []
        for obj_name, obj in model.data_objects.items():
            for i, join in enumerate(obj.joins):
                sides = [(obj_name, col) for col in join.columns_from]
                sides += [(join.join_to, col) for col in join.columns_to]
                for side, col_name in sides:
                    read = model.column_reference_objects(side, col_name)
                    if not read:
                        continue
                    reads = ", ".join(f"'{name}'" for name in sorted(read))
                    errors.append(
                        SemanticError(
                            code="CROSS_OBJECT_JOIN_KEY",
                            message=(
                                f"Join '{obj_name}' → '{join.join_to}' uses computed column "
                                f"'{col_name}' on '{side}' as a key, but its expression reads "
                                f"{reads}. A join key cannot depend on another data object."
                            ),
                            path=f"dataObjects.{obj_name}.joins[{i}]",
                            hint=(
                                "The ON clause is evaluated as the join is made, so a key "
                                f"reading {reads} would need that object joined first — which "
                                "is circular when it is reachable only through this join. Use "
                                f"a physical column of '{side}', or an expression over its own "
                                "columns."
                            ),
                        )
                    )
        return errors

    def _computed_column_graph(self, model: SemanticModel) -> nx.DiGraph[str]:
        """Dependency graph over every computed column in *model*.

        Nodes are ``"Data Object.Column"``; an edge ``a -> b`` means computed
        column ``a``'s expression references ``b``, whether as a ``{sibling}``
        or as a qualified ``{[Data Object].[Column]}``. Only computed columns
        become nodes: a reference to a physical column terminates the chain and
        cannot be part of a cycle.
        """
        g: nx.DiGraph[str] = nx.DiGraph()
        computed = {
            (obj_name, col_name)
            for obj_name, obj in model.data_objects.items()
            for col_name, col in obj.columns.items()
            if col.expression
        }
        for obj_name, col_name in computed:
            g.add_node(f"{obj_name}.{col_name}")
        for obj_name, col_name in computed:
            expression = model.data_objects[obj_name].columns[col_name].expression or ""
            refs = [(obj_name, sibling) for sibling in find_placeholders(expression)]
            refs.extend(find_qualified_refs(expression))
            for ref in refs:
                if ref in computed:
                    g.add_edge(f"{obj_name}.{col_name}", f"{ref[0]}.{ref[1]}")
        return g

    @staticmethod
    def _describe_cycle(g: nx.DiGraph[str], scc: set[str]) -> list[str]:
        """A readable ``a -> b -> a`` walk through one cyclic component."""
        try:
            edges = nx.find_cycle(g.subgraph(scc))
        except nx.NetworkXNoCycle:  # pragma: no cover - scc is cyclic by construction
            return sorted(scc)
        return [source for source, _target in edges] + [edges[0][0]]

    def _build_directed_graph(self, model: SemanticModel) -> nx.DiGraph[str]:
        """Build a directed graph from primary (non-secondary) joins."""
        g: nx.DiGraph[str] = nx.DiGraph()
        for name in model.data_objects:
            g.add_node(name)
        for obj_name, obj in model.data_objects.items():
            for join in obj.joins:
                if not join.secondary and join.join_to in model.data_objects:
                    g.add_edge(obj_name, join.join_to)
        return g

    def _check_via_reachability(self, model: SemanticModel) -> list[SemanticError]:
        """Validate that each dimension's dataObject is reachable from its via."""
        errors: list[SemanticError] = []
        dims_with_via = [(name, dim) for name, dim in model.dimensions.items() if dim.via]
        if not dims_with_via:
            return errors

        g = self._build_directed_graph(model)
        for name, dim in dims_with_via:
            if dim.via not in model.data_objects:
                errors.append(
                    SemanticError(
                        code="INVALID_VIA_DATA_OBJECT",
                        message=(
                            f"Dimension '{name}': via references unknown data object '{dim.via}'"
                        ),
                        path=f"dimensions.{name}",
                    )
                )
                continue
            if dim.via == dim.view:
                continue
            reachable = nx.descendants(g, dim.via) if dim.via in g else set()
            if dim.view not in reachable:
                errors.append(
                    SemanticError(
                        code="INVALID_VIA_DATA_OBJECT",
                        message=(
                            f"Dimension '{name}': data object '{dim.view}' is not "
                            f"reachable from via data object '{dim.via}'"
                        ),
                        path=f"dimensions.{name}",
                    )
                )
        return errors

    def _check_measure_anchors(self, model: SemanticModel) -> list[SemanticError]:
        """Validate each measure's ``anchor``: it must exist and be one it reads.

        The anchor names the data object whose rows the expression is evaluated
        over, so an anchor the expression never reads would leave every column
        conformed in and the anchor acting as a bare row multiplier. That is
        never what was meant, and it is what a typo looks like.
        """
        errors: list[SemanticError] = []
        for name, measure in model.measures.items():
            if not measure.anchor:
                continue
            if measure.anchor not in model.data_objects:
                errors.append(
                    SemanticError(
                        code="INVALID_ANCHOR_DATA_OBJECT",
                        message=(
                            f"Measure '{name}': anchor references unknown data object "
                            f"'{measure.anchor}'"
                        ),
                        path=f"measures.{name}",
                    )
                )
                continue
            sources = measure.source_objects
            if not sources or measure.anchor in sources:
                continue
            # An anchor may also name a data object every source joins to: that
            # conforms all of them to its grain, which is the reading a model
            # picks when the facts share several dimensions and no single one
            # can be assumed.
            shared = model.common_join_targets(sorted(sources))
            if measure.anchor in shared:
                continue
            options = sorted(sources) + shared
            errors.append(
                SemanticError(
                    code="INVALID_ANCHOR_DATA_OBJECT",
                    message=(
                        f"Measure '{name}': anchor '{measure.anchor}' is neither a data object "
                        f"it reads nor one they all join to. The anchor sets the grain the "
                        f"expression is evaluated at, so it has to be one of: "
                        f"{', '.join(options)}."
                    ),
                    path=f"measures.{name}",
                )
            )
        return errors

    def _check_missing_via(self, model: SemanticModel) -> list[SemanticError]:
        """Warn when a dimension's target has direct joins from multiple fact tables.

        A fact table is a data object that is the source of at least one measure.
        Only direct joins (one hop) from a fact table to the dimension's target
        count — transitive reachability through other fact tables does not create
        real ambiguity and should not trigger a warning.  Dimensions whose target
        IS a fact table (e.g. Sales Date on Sales) are also skipped because the
        column lives on the fact table itself.

        Path-invariance heuristic: when every reaching fact joins to the target
        on the target's primary key, the dim attribute is path-invariant — the
        same Client ID (or Calendar.date) from any fact resolves to the same
        target row, so the dim attribute value is identical regardless of
        which fact drove the join. Role-playing semantics (Sales Year Month
        vs Purchase Year Month) are a choice the modeller makes by adding
        explicit ``via:`` on a per-dimension basis, not a correctness concern
        the validator should flag for every shared dim table.
        """
        warnings: list[SemanticError] = []

        measure_sources: set[str] = set()
        for meas in model.measures.values():
            for col_ref in meas.columns:
                if col_ref.view:
                    measure_sources.add(col_ref.view)
        if len(measure_sources) < 2:
            return warnings

        g = self._build_directed_graph(model)
        fact_tables = sorted(measure_sources & set(g.nodes))

        direct_children: dict[str, set[str]] = {}
        for ft in fact_tables:
            direct_children[ft] = set(g.successors(ft))

        for dim_name, dim in model.dimensions.items():
            if dim.via:
                continue
            target = dim.view
            if not target or target not in g:
                continue
            if target in measure_sources:
                continue
            reaching_facts = [ft for ft in fact_tables if target in direct_children[ft]]
            if len(reaching_facts) <= 1:
                continue

            if self._is_path_invariant(model, target, reaching_facts):
                continue

            warnings.append(
                SemanticError(
                    code="MISSING_VIA",
                    message=(
                        f"Dimension '{dim_name}' on '{target}' has direct "
                        f"joins from multiple fact tables "
                        f"({', '.join(reaching_facts)}). "
                        f"Consider adding role-playing dimensions with 'via' "
                        f"to disambiguate join paths."
                    ),
                    path=f"dimensions.{dim_name}",
                    severity="warning",
                )
            )
        return warnings

    @staticmethod
    def _is_path_invariant(model: SemanticModel, target: str, reaching_facts: list[str]) -> bool:
        """True when every reaching fact joins to the target on its primary key.

        Same Client ID (or Calendar date) from any fact resolves to the same
        target row, so the dim attribute value is identical regardless of which
        fact drove the join — there's no correctness ambiguity to warn about.
        Joins on non-PK columns CAN resolve to different rows from different
        facts and are kept under the warning.
        """
        target_obj = model.data_objects.get(target)
        if target_obj is None:
            return False

        pk_cols = {col_name for col_name, col in target_obj.columns.items() if col.primary_key}
        if not pk_cols:
            return False

        for ft_name in reaching_facts:
            ft_obj = model.data_objects.get(ft_name)
            if ft_obj is None:
                return False
            joins_to_target = [j for j in ft_obj.joins if j.join_to == target]
            if not joins_to_target:
                return False
            for j in joins_to_target:
                # Every column on the target side of the join must be a PK column.
                if not j.columns_to or any(c not in pk_cols for c in j.columns_to):
                    return False

        return True

"""Semantic validation: cycles, ambiguous joins, reference integrity (spec §3.8)."""

from __future__ import annotations

from collections import deque

import networkx as nx

from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import (
    DataType,
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    SemanticModel,
)
from orionbelt.models.synthesis import count_label, model_count_pattern


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
        errors.extend(self._check_via_reachability(model))
        errors.extend(self._check_missing_via(model))
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

    def _check_measures_resolve(self, model: SemanticModel) -> list[SemanticError]:
        """Ensure measure column references resolve to actual data object columns."""
        errors: list[SemanticError] = []
        for name, measure in model.measures.items():
            for i, col_ref in enumerate(measure.columns):
                obj_name = col_ref.view
                col_name = col_ref.column
                if obj_name and obj_name not in model.data_objects:
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_DATA_OBJECT",
                            message=(
                                f"Measure '{name}' column[{i}] references "
                                f"unknown data object '{obj_name}'"
                            ),
                            path=f"measures.{name}.columns[{i}]",
                        )
                    )
                elif obj_name and col_name:
                    obj = model.data_objects[obj_name]
                    if col_name not in obj.columns:
                        errors.append(
                            SemanticError(
                                code="UNKNOWN_COLUMN",
                                message=(
                                    f"Measure '{name}' column[{i}] references "
                                    f"unknown column '{col_name}' in data object '{obj_name}'"
                                ),
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
            obj_name = dim.view
            col_name = dim.column
            if obj_name and obj_name not in model.data_objects:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_DATA_OBJECT",
                        message=f"Dimension '{name}' references unknown data object '{obj_name}'",
                        path=f"dimensions.{name}",
                    )
                )
            elif obj_name and col_name:
                obj = model.data_objects[obj_name]
                if col_name not in obj.columns:
                    errors.append(
                        SemanticError(
                            code="UNKNOWN_COLUMN",
                            message=(
                                f"Dimension '{name}' references unknown column "
                                f"'{col_name}' in data object '{obj_name}'"
                            ),
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
            if not item.column or not item.column.view:
                return
            obj = model.data_objects.get(item.column.view)
            if not obj:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_FILTER_DATA_OBJECT",
                        message=(
                            f"Measure '{meas_name}' filter references unknown "
                            f"data object '{item.column.view}'"
                        ),
                        path=f"measures.{meas_name}.filters",
                    )
                )
                return
            if item.column.column and item.column.column not in obj.columns:
                errors.append(
                    SemanticError(
                        code="UNKNOWN_FILTER_COLUMN",
                        message=(
                            f"Measure '{meas_name}' filter references unknown "
                            f"column '{item.column.column}' in '{item.column.view}'"
                        ),
                        path=f"measures.{meas_name}.filters",
                    )
                )
        elif isinstance(item, MeasureFilterGroup):
            for child in item.filters:
                self._validate_filter_item(child, model, meas_name, errors)

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

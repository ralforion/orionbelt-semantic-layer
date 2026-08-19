"""Join graph: data objects as nodes, joins as edges. Uses networkx for path resolution."""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

from orionbelt.ast.nodes import BinaryOp, ColumnRef, Expr
from orionbelt.ast.nodes import JoinType as ASTJoinType
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import UsePathName
from orionbelt.models.semantic import Cardinality, DataObject, SemanticModel


@dataclass
class JoinStep:
    """A single step in a resolved join path."""

    from_object: str
    to_object: str
    from_columns: list[str]
    to_columns: list[str]
    join_type: ASTJoinType
    cardinality: Cardinality
    reversed: bool = False
    nested: bool = False
    """``to_object`` is unnested from an array column on ``from_object``.

    Not a join at all: there is no key and no predicate, because the elements
    were inside the parent's row before any SQL ran. The step is still a step
    because everything downstream - path finding, fanout, the planner's join
    loop - reasons about the query as a walk over the graph, and containment is
    an edge of that walk however it is rendered.
    """


def _join_type_for(edge_data: dict[str, object]) -> ASTJoinType:
    """LEFT unless the join declares the match mandatory.

    ``required: true`` says a row without a match on the other side is
    meaningless, which is what an INNER JOIN encodes. Everything else keeps the
    LEFT default, where an unmatched row survives with NULLs.
    """
    return ASTJoinType.INNER if edge_data.get("required") else ASTJoinType.LEFT


def path_overrides(use_path_names: list[UsePathName] | None) -> dict[tuple[str, str], str]:
    """``(source, target)`` to the secondary ``pathName`` the query selected.

    Shared with :meth:`~orionbelt.models.semantic.SemanticModel.effective_joins`
    so graph traversal and anything else reading join columns agree on which
    joins a query is actually using.
    """
    return {(upn.source, upn.target): upn.path_name for upn in use_path_names or ()}


class JoinGraph:
    """Graph of data objects (nodes) and relationships (edges) for join path resolution."""

    def __init__(
        self,
        model: SemanticModel,
        use_path_names: list[UsePathName] | None = None,
    ) -> None:
        self._graph: nx.Graph[str] = nx.Graph()
        self._directed: nx.DiGraph[str] = nx.DiGraph()
        # Path-finding graph: many-to-one is forward-only (would cause fanout
        # in reverse); one-to-one and many-to-many are bidirectional.
        self._traversable: nx.DiGraph[str] = nx.DiGraph()
        self._model = model
        self._build(model, use_path_names)

    def _build(
        self,
        model: SemanticModel,
        use_path_names: list[UsePathName] | None = None,
    ) -> None:
        """Build the graph from the semantic model.

        Secondary joins are only included when their pathName is requested
        via *use_path_names*.  When a secondary override is active for a
        ``(source, target)`` pair, the primary join for that pair is excluded.
        """
        for name in model.data_objects:
            self._graph.add_node(name)
            self._directed.add_node(name)
            self._traversable.add_node(name)

        active_overrides = path_overrides(use_path_names)

        for obj_name, obj in model.data_objects.items():
            for join in obj.joins:
                if join.join_to not in model.data_objects:
                    continue
                # A nested object's declared join to its own parent is the
                # ``code`` fallback's join, not a second route: containment
                # already connects the two, and adding both would put a cycle
                # between them. Its columns are folded onto the nested edge
                # below, which is where the fallback reads them from.
                if obj.nested_in is not None and join.join_to == obj.nested_in.data_object:
                    continue
                pair = (obj_name, join.join_to)

                if join.secondary:
                    # Only include if this secondary join's pathName is active
                    if pair in active_overrides and active_overrides[pair] == join.path_name:
                        self._add_edge(obj_name, join)
                else:
                    # Primary join: skip if an active override exists for this pair
                    if pair not in active_overrides:
                        self._add_edge(obj_name, join)

        for obj_name, obj in model.data_objects.items():
            if obj.nested_in is not None and obj.nested_in.data_object in model.data_objects:
                self._add_nested_edge(obj_name, obj)

    def _add_edge(self, obj_name: str, join: object) -> None:
        """Add an edge to the undirected, directed, and traversable graphs.

        The traversable graph is used by :meth:`find_join_path` to enforce
        the rule "many-to-one is never bidirectional": walking such a join
        backwards would multiply rows of the source table, so only forward
        traversal is allowed.  One-to-one and many-to-many joins remain
        bidirectional in the traversable graph.
        """
        from orionbelt.models.semantic import DataObjectJoin

        assert isinstance(join, DataObjectJoin)
        self._graph.add_edge(
            obj_name,
            join.join_to,
            columns_from=join.columns_from,
            columns_to=join.columns_to,
            cardinality=join.join_type,
            source_object=obj_name,
            required=join.required,
        )
        self._directed.add_edge(
            obj_name,
            join.join_to,
            columns_from=join.columns_from,
            columns_to=join.columns_to,
            cardinality=join.join_type,
        )
        self._traversable.add_edge(obj_name, join.join_to)
        if join.join_type != Cardinality.MANY_TO_ONE:
            # Safe to walk backwards: row count is preserved.
            self._traversable.add_edge(join.join_to, obj_name)

    def _add_nested_edge(self, obj_name: str, obj: DataObject) -> None:
        """Add the edge a ``nestedIn`` object declares by containment.

        Oriented **parent to child**, which is the opposite of how the object
        reads: the child is the many side, but only the parent can put it in
        scope, so the parent is what the walk starts from and what
        :meth:`find_common_root` has to answer. Traversal is one-way for the
        same reason - there is nothing to reach by leaving a nested object that
        its parent does not already reach.

        The edge is many-to-one *declared child to parent*, so walking it in the
        direction stored here multiplies the parent's rows. That is exactly what
        an unnest does, and ``nested`` is what tells fanout detection and the
        planner so, since neither the cardinality nor ``reversed`` can say it:
        the multiplication comes out of the FROM clause rather than a predicate.

        ``columns_from`` / ``columns_to`` are the *fallback* join's, oriented
        parent-first to match the edge. They are empty unless the object also
        declares ``code`` and a join to its parent, and are read only where the
        dialect cannot unnest.
        """
        source = obj.nested_in
        if source is None:
            return
        parent = source.data_object
        fallback = next((j for j in obj.joins if j.join_to == parent), None)
        columns_from = list(fallback.columns_to) if fallback else []
        columns_to = list(fallback.columns_from) if fallback else []
        self._graph.add_edge(
            parent,
            obj_name,
            columns_from=columns_from,
            columns_to=columns_to,
            cardinality=Cardinality.MANY_TO_ONE,
            source_object=parent,
            # An empty array keeps its parent row, which is a LEFT join: a
            # charge carrying no labels still contributes its cost to an
            # unfiltered total, and 61% of the rows in a real billing export
            # carry none.
            required=False,
            nested=True,
        )
        self._directed.add_edge(
            parent,
            obj_name,
            columns_from=columns_from,
            columns_to=columns_to,
            cardinality=Cardinality.MANY_TO_ONE,
            nested=True,
        )
        self._traversable.add_edge(parent, obj_name)

    def _unnest_root(self, name: str) -> str:
        """The nearest ancestor of *name* a FROM clause can name.

        Delegates to the model, which is where the same question is asked from
        query resolution - a nested object must not be picked as a base object
        either, and the two answers have to be the same one.
        """
        return self._model.unnest_root(name)

    def descendants(self, node: str) -> set[str]:
        """Return all nodes reachable from *node* via directed join paths."""
        if node not in self._directed:
            return set()
        return nx.descendants(self._directed, node)

    def find_common_root(self, required_objects: set[str]) -> str:
        """Find the common root for a set of required objects.

        The join graph is a DAG (joins define direction: source → joinTo).
        The common root is the **deepest** node that can reach ALL
        *required_objects* via directed join paths.  "Deepest" = smallest
        descendant set (most specific ancestor, closest to the required nodes).

        In ``returns → sales → customer``, with required ``{customer, item}``,
        the common root is ``sales`` (it can reach both).  With required
        ``{customer, item, returns}``, the common root is ``returns`` (the
        only node that can reach all three).
        """
        required = required_objects & set(self._directed.nodes)
        if len(required) <= 1:
            return self._unnest_root(next(iter(sorted(required)))) if required else ""

        # Find all nodes that can reach ALL required nodes via directed paths
        candidates: list[tuple[str, int]] = []
        for node in self._directed.nodes:
            reachable = nx.descendants(self._directed, node) | {node}
            if required <= reachable:
                candidates.append((node, len(reachable)))

        if not candidates:
            # Fallback: no single directed ancestor covers all —
            # use undirected shortest-path center
            return self._find_center_undirected(required)

        # Pick the deepest ancestor: smallest reachable set that still covers all
        candidates.sort(key=lambda x: (x[1], x[0]))
        return self._unnest_root(candidates[0][0])

    def _find_center_undirected(self, required: set[str]) -> str:
        """Fallback: center of the Steiner tree in the undirected graph."""
        nodes = sorted(required)
        if len(nodes) <= 1:
            return nodes[0] if nodes else ""

        steiner: set[str] = set()
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                try:
                    path: list[str] = nx.shortest_path(self._graph, nodes[i], nodes[j])
                    steiner.update(path)
                except nx.NetworkXNoPath:
                    pass

        if not steiner:
            return self._unnest_root(nodes[0])

        # ``nodes`` can span disconnected components — the pairwise loop above
        # simply skips those pairs, so a Steiner node need not reach every
        # required node. Score an unreachable target as worse than any real
        # distance instead of letting ``shortest_path_length`` raise.
        unreachable = len(self._graph.nodes) + 1

        def _eccentricity(node: str) -> int:
            worst: int = 0
            for target in nodes:
                try:
                    # Unweighted graph, so the hop count is always integral;
                    # the stub types it as float to cover weighted callers.
                    worst = max(worst, int(nx.shortest_path_length(self._graph, node, target)))
                except nx.NetworkXNoPath:
                    worst = max(worst, unreachable)
            return worst

        best: str = nodes[0]
        best_max: int = unreachable + 1
        for node in sorted(steiner):
            max_dist = _eccentricity(node)
            if max_dist < best_max:
                best_max = max_dist
                best = node
        return self._unnest_root(best)

    def _hops_from(self, origin: str | None, node: str) -> int:
        """Hops from *origin* to *node* in the traversable graph.

        ``0`` when they are the same or no origin was given, and a value larger
        than any real path when *node* is out of reach — so an unreachable
        source sorts last rather than raising.
        """
        if origin is None or origin == node:
            return 0
        try:
            return int(nx.shortest_path_length(self._traversable, origin, node))
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return len(self._traversable) + 1

    def role_candidates(
        self,
        from_objects: set[str],
        to_object: str,
        prefer_from: str | None = None,
    ) -> list[list[str]]:
        """The equally-good ways to reach *to_object*, one path each.

        A data object joined by more than one of the objects already in the
        query is reachable by more than one *role* — ``date_dim`` as the sold
        date and as the returned date, ``warehouse`` as the sale's and as the
        inventory's. Which role a plain reference means is a question the model
        cannot answer, so the caller has to know when there is a real choice.

        Candidates are ranked by path length first, then by how far the source
        sits from *prefer_from* (the query's base object). That makes the
        answer both deterministic and principled: the base is what the query is
        anchored on, so the role reached from it directly beats one reached
        through another fact. Ranking by nothing, as this did before, left the
        choice to set iteration order — the same query then compiled to a
        different role from run to run, since string hashing is randomised per
        process.

        Returns every path tied at the best rank that enters *to_object* by a
        *different* join, deduplicated on that last edge: two routes arriving
        on the same key are the same role, however they got there. One element
        means the choice is clear; more than one means it is genuinely
        ambiguous and only the query can resolve it.
        """
        ranked: list[tuple[tuple[int, int, str], list[str]]] = []
        for source in sorted(from_objects):
            if source not in self._traversable or to_object not in self._traversable:
                continue
            lengths = nx.single_source_shortest_path_length(self._traversable, source)
            distance = lengths.get(to_object)
            if distance is None:
                continue
            # What separates one role from another is the *last* edge — which
            # join actually lands on the target. Every predecessor sitting one
            # hop closer to the source is the last edge of some shortest path,
            # so one traversal yields the complete set of roles. Enumerating
            # the paths instead would need a cap, and a cap applied before
            # roles are distinguished can hide one behind many routes that
            # share an entry.
            for entry in sorted(self._traversable.predecessors(to_object)):
                if lengths.get(entry) != distance - 1:
                    continue
                prefix: list[str] = nx.shortest_path(self._traversable, source, entry)
                candidate = [*prefix, to_object]
                rank = (len(candidate), self._hops_from(prefer_from, source), source)
                ranked.append((rank, candidate))
        if not ranked:
            return []

        best = min(rank for rank, _ in ranked)[:2]
        by_entry: dict[tuple[str, str], list[str]] = {}
        for rank, candidate in sorted(ranked):
            if rank[:2] != best:
                continue
            last_edge = (
                (candidate[-2], candidate[-1])
                if len(candidate) > 1
                else (candidate[0], candidate[0])
            )
            by_entry.setdefault(last_edge, candidate)
        return list(by_entry.values())

    def find_join_path(
        self,
        from_objects: set[str],
        to_objects: set[str],
        via_constraints: dict[str, str] | None = None,
        prefer_from: str | None = None,
        ambiguous: dict[str, list[list[str]]] | None = None,
    ) -> list[JoinStep]:
        """Find a minimal join path connecting all required data objects.

        Uses shortest path for each target object from the set of source
        objects, ranked by :meth:`role_candidates` so the choice among several
        reachable roles is deterministic rather than set-order dependent.

        *via_constraints* maps ``target → via``: for constrained targets, only
        the ``via`` object is used as the source so the path is forced through it.
        *prefer_from* is the query's base object, which breaks ties in favour of
        the role it reaches directly.

        A path is still produced when several roles tie, because most callers
        only need *a* join. Pass *ambiguous* to learn about it: each target that
        tied is recorded there with the routes it tied between, so a caller that
        must not guess — query resolution — can refuse instead.
        """
        steps: list[JoinStep] = []
        visited_edges: set[tuple[str, str]] = set()
        via = via_constraints or {}

        # Process via waypoints first so they are in source_list when their
        # constrained targets are processed.
        all_targets = to_objects - from_objects
        via_targets = {t for t in all_targets if t in via}
        non_via_targets = all_targets - via_targets
        via_waypoints = {via[t] for t in via_targets} - from_objects - via_targets
        ordered_targets = sorted(via_waypoints) + sorted(non_via_targets) + sorted(via_targets)

        source_list = list(from_objects)
        # A single starting object *is* the anchor — that is how the planner
        # calls this, from the query's base. Without it the tie-break falls
        # back to the source name, which is deterministic but arbitrary: it
        # bound a date filter to whichever fact sorted first.
        anchor = prefer_from or (next(iter(from_objects)) if len(from_objects) == 1 else None)

        for target in ordered_targets:
            sources = [via[target]] if target in via and via[target] in source_list else source_list
            candidates = self.role_candidates(set(sources), target, prefer_from=anchor)
            if len(candidates) > 1 and ambiguous is not None:
                ambiguous[target] = candidates
            best_path = candidates[0] if candidates else None

            if best_path is None:
                continue

            for i in range(len(best_path) - 1):
                edge = (best_path[i], best_path[i + 1])
                rev_edge = (best_path[i + 1], best_path[i])
                if edge in visited_edges or rev_edge in visited_edges:
                    continue
                visited_edges.add(edge)

                edge_data = self._graph.edges[edge]
                source_object = edge_data.get("source_object", edge[0])

                if source_object == edge[0]:
                    step = JoinStep(
                        from_object=edge[0],
                        to_object=edge[1],
                        from_columns=edge_data["columns_from"],
                        to_columns=edge_data["columns_to"],
                        join_type=_join_type_for(edge_data),
                        cardinality=edge_data["cardinality"],
                        nested=bool(edge_data.get("nested")),
                    )
                else:
                    # Path traverses the edge against its declared direction.
                    # ``JoinStep`` keeps from/to in *declared* order — ``edge[1]``
                    # is the object that declares the join, so it keeps
                    # ``columns_from`` — and records the real traversal
                    # direction in ``reversed``.
                    step = JoinStep(
                        from_object=edge[1],
                        to_object=edge[0],
                        from_columns=edge_data["columns_from"],
                        to_columns=edge_data["columns_to"],
                        join_type=_join_type_for(edge_data),
                        cardinality=edge_data["cardinality"],
                        reversed=True,
                        nested=bool(edge_data.get("nested")),
                    )
                steps.append(step)

            # Add target to sources for subsequent lookups
            if target not in source_list:
                source_list.append(target)

        return steps

    def find_join_path_undirected(
        self,
        from_object: str,
        to_object: str,
    ) -> list[JoinStep]:
        """Find a join path ignoring cardinality direction.

        Unlike :meth:`find_join_path` (which forbids walking many-to-one
        joins backwards to prevent fanout in the outer query), this walker
        considers the join graph as undirected.  It's intended for
        correlated subqueries — EXISTS / NOT EXISTS — where row counts on
        the outer side are unaffected by how many rows the subquery scans.

        Each emitted :class:`JoinStep` is oriented so ``from_object`` is the
        step's predecessor on the path and ``to_object`` is its successor;
        ``from_columns`` / ``to_columns`` are swapped when the underlying
        join edge is traversed against its declared direction.
        """
        if from_object == to_object:
            return []
        if from_object not in self._graph or to_object not in self._graph:
            return []
        try:
            path: list[str] = nx.shortest_path(self._graph, from_object, to_object)
        except nx.NetworkXNoPath:
            return []

        steps: list[JoinStep] = []
        for i in range(len(path) - 1):
            pred, succ = path[i], path[i + 1]
            edge_data = self._graph.edges[(pred, succ)]
            source_object = edge_data.get("source_object", pred)
            if source_object == pred:
                from_cols = edge_data["columns_from"]
                to_cols = edge_data["columns_to"]
                reversed_ = False
            else:
                from_cols = edge_data["columns_to"]
                to_cols = edge_data["columns_from"]
                reversed_ = True
            steps.append(
                JoinStep(
                    from_object=pred,
                    to_object=succ,
                    from_columns=from_cols,
                    to_columns=to_cols,
                    join_type=_join_type_for(edge_data),
                    cardinality=edge_data["cardinality"],
                    reversed=reversed_,
                    nested=bool(edge_data.get("nested")),
                )
            )
        return steps

    def build_join_condition(self, step: JoinStep) -> Expr:
        """Build the ON clause expression for a join step.

        Routes both sides through ``make_column_expr`` so a computed
        join key (``expression:`` instead of ``code:`` on the column)
        inlines its template body. Without this, a join on a computed
        key would render ``"obj"."" = "other"."key"`` and the database
        would error on the zero-length identifier.

        The model's query time zone is deliberately *not* applied here. A join
        asks whether two rows belong together, which no calendar changes: both
        sides would convert identically and the answer would be the same, at
        the cost of wrapping a join key in a function, which is how an index or
        a partition stops being used. Conversion exists so that bucketing and
        display happen in the model's frame, and an ON clause is neither.
        """
        from orionbelt.compiler.resolution import make_column_expr

        conditions: list[Expr] = []
        for from_c, to_c in zip(step.from_columns, step.to_columns, strict=True):
            from_obj = self._model.data_objects.get(step.from_object)
            to_obj = self._model.data_objects.get(step.to_object)
            if from_obj and from_c in from_obj.columns:
                left_expr: Expr = make_column_expr(
                    self._model, step.from_object, from_c, in_query_timezone=False
                )
            else:
                left_expr = ColumnRef(name=from_c, table=step.from_object)
            if to_obj and to_c in to_obj.columns:
                right_expr: Expr = make_column_expr(
                    self._model, step.to_object, to_c, in_query_timezone=False
                )
            else:
                right_expr = ColumnRef(name=to_c, table=step.to_object)
            conditions.append(BinaryOp(left=left_expr, op="=", right=right_expr))

        if not conditions:
            msg = f"Join from '{step.from_object}' to '{step.to_object}' has no join columns"
            raise ValueError(msg)
        result: Expr = conditions[0]
        for cond in conditions[1:]:
            result = BinaryOp(left=result, op="AND", right=cond)
        return result

    def detect_cycles(self) -> list[list[str]]:
        """Detect cyclic join paths."""
        try:
            cycles = list(nx.simple_cycles(self._directed))
            return cycles
        except nx.NetworkXError:
            return []

    def validate_deterministic(self) -> list[SemanticError]:
        """Ensure join paths are deterministic (no ambiguity)."""
        errors: list[SemanticError] = []
        # Check for multiple edges between the same pair of nodes
        for u, v in self._graph.edges():
            if self._graph.number_of_edges(u, v) > 1:
                errors.append(
                    SemanticError(
                        code="AMBIGUOUS_JOIN",
                        message=f"Multiple join paths between '{u}' and '{v}'",
                        path=f"dataObjects.{u}.joins",
                    )
                )
        return errors

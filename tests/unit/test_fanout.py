"""Tests for fanout detection."""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import ColumnRef, FunctionCall
from orionbelt.ast.nodes import JoinType as ASTJoinType
from orionbelt.compiler.fanout import FanoutError, _step_causes_fanout, detect_fanout
from orionbelt.compiler.graph import JoinStep
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import (
    ResolutionError,
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
)
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import (
    Cardinality,
    DataObject,
    DataObjectColumn,
    DataObjectJoin,
    DataType,
    Dimension,
    Measure,
    Metric,
    SemanticModel,
)

# -- helpers -----------------------------------------------------------------


def _make_model(
    *,
    cardinality: Cardinality = Cardinality.MANY_TO_ONE,
    allow_fan_out: bool = False,
    add_metric: bool = False,
    measure_on_customers: bool = False,
) -> SemanticModel:
    """Build a minimal two-object model with configurable join cardinality.

    When ``measure_on_customers=True``, the measure references Customers
    instead of Orders, simulating a reversed-traversal fanout scenario.
    """
    orders = DataObject(
        label="Orders",
        code="ORDERS",
        database="WH",
        schema_name="PUBLIC",
        columns={
            "Order ID": DataObjectColumn(
                label="Order ID", code="ORDER_ID", abstract_type=DataType.STRING
            ),
            "Customer ID": DataObjectColumn(
                label="Customer ID", code="CUSTOMER_ID", abstract_type=DataType.STRING
            ),
            "Amount": DataObjectColumn(label="Amount", code="AMOUNT", abstract_type=DataType.FLOAT),
        },
        joins=[
            DataObjectJoin(
                join_type=cardinality,
                join_to="Customers",
                columns_from=["Customer ID"],
                columns_to=["Cust ID"],
            )
        ],
    )
    customers = DataObject(
        label="Customers",
        code="CUSTOMERS",
        database="WH",
        schema_name="PUBLIC",
        columns={
            "Cust ID": DataObjectColumn(
                label="Cust ID", code="CUST_ID", abstract_type=DataType.STRING
            ),
            "Country": DataObjectColumn(
                label="Country", code="COUNTRY", abstract_type=DataType.STRING
            ),
            "Revenue": DataObjectColumn(
                label="Revenue", code="REVENUE", abstract_type=DataType.FLOAT
            ),
        },
    )

    if measure_on_customers:
        measure_obj, measure_col = "Customers", "Revenue"
    else:
        measure_obj, measure_col = "Orders", "Amount"

    measures: dict[str, Measure] = {
        "Total Revenue": Measure(
            label="Total Revenue",
            columns=[{"dataObject": measure_obj, "column": measure_col}],
            result_type=DataType.FLOAT,
            aggregation="sum",
            allow_fan_out=allow_fan_out,
        ),
    }
    metrics: dict[str, Metric] = {}
    if add_metric:
        measures["Order Count"] = Measure(
            label="Order Count",
            columns=[{"dataObject": measure_obj, "column": measure_col}],
            result_type=DataType.INT,
            aggregation="count",
        )
        metrics["Revenue per Order"] = Metric(
            label="Revenue per Order",
            expression="{[Total Revenue]} / {[Order Count]}",
        )

    return SemanticModel(
        data_objects={"Orders": orders, "Customers": customers},
        dimensions={
            "Customer Country": Dimension(
                label="Customer Country",
                view="Customers",
                column="Country",
                result_type=DataType.STRING,
            ),
        },
        measures=measures,
        metrics=metrics,
    )


def _make_resolved(
    model: SemanticModel,
    *,
    reversed_step: bool = False,
    cardinality: Cardinality = Cardinality.MANY_TO_ONE,
    measure_names: list[str] | None = None,
    component_measures: list[str] | None = None,
) -> ResolvedQuery:
    """Build a minimal resolved query with one join step.

    When ``reversed_step=False`` (forward): traversal goes Orders→Customers,
    matching the declared join direction.  ``from_object="Orders"``,
    ``to_object="Customers"``.

    When ``reversed_step=True`` (reversed): traversal goes Customers→Orders
    (against declared direction).  ``find_join_path`` swaps from/to to keep
    the declared direction: ``from_object="Orders"``, ``to_object="Customers"``.
    The measure is on Customers (``to_object``), which is the actual traversal
    origin — the side whose rows get multiplied.
    """
    if measure_names is None:
        measure_names = ["Total Revenue"]

    # When reversed, the measure's source object is the actual traversal
    # origin (to_object in the JoinStep), which is Customers.
    measure_source = "Customers" if reversed_step else "Orders"

    measures: list[ResolvedMeasure] = []
    for mname in measure_names:
        model_m = model.measures.get(mname)
        if model_m:
            measures.append(
                ResolvedMeasure(
                    name=mname,
                    aggregation=model_m.aggregation,
                    expression=FunctionCall(
                        name=model_m.aggregation.upper(),
                        args=[ColumnRef(name="AMOUNT", table=measure_source)],
                    ),
                )
            )
        elif mname in model.metrics:
            measures.append(
                ResolvedMeasure(
                    name=mname,
                    aggregation="",
                    expression=ColumnRef(name=mname),
                    component_measures=component_measures or [],
                    is_expression=True,
                )
            )

    # JoinStep from_object/to_object always follow declared direction.
    # reversed=True indicates actual traversal is to_object→from_object.
    join_steps = [
        JoinStep(
            from_object="Orders",
            to_object="Customers",
            from_columns=["Customer ID"],
            to_columns=["Cust ID"],
            join_type=ASTJoinType.LEFT,
            cardinality=cardinality,
            reversed=reversed_step,
        ),
    ]

    return ResolvedQuery(
        dimensions=[
            ResolvedDimension(
                name="Customer Country",
                object_name="Customers",
                column_name="Country",
                source_column="COUNTRY",
            ),
        ],
        measures=measures,
        base_object=measure_source,
        required_objects={"Orders", "Customers"},
        join_steps=join_steps,
        measure_source_objects={measure_source},
    )


# -- _step_causes_fanout unit tests -----------------------------------------


class TestStepCausesFanout:
    def test_many_to_one_forward_safe(self) -> None:
        step = JoinStep(
            from_object="A",
            to_object="B",
            from_columns=["x"],
            to_columns=["y"],
            join_type=ASTJoinType.LEFT,
            cardinality=Cardinality.MANY_TO_ONE,
            reversed=False,
        )
        assert _step_causes_fanout(step) is False

    def test_many_to_one_reversed_fanout(self) -> None:
        step = JoinStep(
            from_object="B",
            to_object="A",
            from_columns=["y"],
            to_columns=["x"],
            join_type=ASTJoinType.LEFT,
            cardinality=Cardinality.MANY_TO_ONE,
            reversed=True,
        )
        assert _step_causes_fanout(step) is True

    def test_one_to_one_forward_safe(self) -> None:
        step = JoinStep(
            from_object="A",
            to_object="B",
            from_columns=["x"],
            to_columns=["y"],
            join_type=ASTJoinType.LEFT,
            cardinality=Cardinality.ONE_TO_ONE,
            reversed=False,
        )
        assert _step_causes_fanout(step) is False

    def test_one_to_one_reversed_safe(self) -> None:
        step = JoinStep(
            from_object="B",
            to_object="A",
            from_columns=["y"],
            to_columns=["x"],
            join_type=ASTJoinType.LEFT,
            cardinality=Cardinality.ONE_TO_ONE,
            reversed=True,
        )
        assert _step_causes_fanout(step) is False

    def test_many_to_many_fanout(self) -> None:
        step = JoinStep(
            from_object="A",
            to_object="B",
            from_columns=["x"],
            to_columns=["y"],
            join_type=ASTJoinType.LEFT,
            cardinality=Cardinality.MANY_TO_MANY,
            reversed=False,
        )
        assert _step_causes_fanout(step) is True


# -- detect_fanout integration tests ----------------------------------------


class TestDetectFanout:
    def test_safe_many_to_one_forward(self) -> None:
        """many-to-one in declared direction: no fanout."""
        model = _make_model(cardinality=Cardinality.MANY_TO_ONE)
        resolved = _make_resolved(model, reversed_step=False, cardinality=Cardinality.MANY_TO_ONE)
        detect_fanout(resolved, model)  # should not raise

    def test_fanout_many_to_one_reversed(self) -> None:
        """many-to-one traversed in reverse = one-to-many: fanout."""
        model = _make_model(cardinality=Cardinality.MANY_TO_ONE, measure_on_customers=True)
        resolved = _make_resolved(model, reversed_step=True, cardinality=Cardinality.MANY_TO_ONE)
        with pytest.raises(FanoutError, match="fanout"):
            detect_fanout(resolved, model)

    def test_safe_one_to_one(self) -> None:
        """one-to-one in any direction: no fanout."""
        model = _make_model(cardinality=Cardinality.ONE_TO_ONE)
        resolved = _make_resolved(model, reversed_step=True, cardinality=Cardinality.ONE_TO_ONE)
        detect_fanout(resolved, model)  # should not raise

    def test_fanout_many_to_many(self) -> None:
        """many-to-many always causes fanout."""
        model = _make_model(cardinality=Cardinality.MANY_TO_MANY)
        resolved = _make_resolved(model, reversed_step=False, cardinality=Cardinality.MANY_TO_MANY)
        with pytest.raises(FanoutError, match="fanout"):
            detect_fanout(resolved, model)

    def test_allow_fan_out_suppresses_error(self) -> None:
        """allowFanOut: true on the measure suppresses the fanout error."""
        model = _make_model(
            cardinality=Cardinality.MANY_TO_ONE,
            allow_fan_out=True,
            measure_on_customers=True,
        )
        resolved = _make_resolved(model, reversed_step=True, cardinality=Cardinality.MANY_TO_ONE)
        detect_fanout(resolved, model)  # should not raise

    def test_metric_with_fanout_component(self) -> None:
        """A metric whose component measure has fanout should raise."""
        model = _make_model(
            cardinality=Cardinality.MANY_TO_ONE,
            add_metric=True,
            measure_on_customers=True,
        )
        resolved = _make_resolved(
            model,
            reversed_step=True,
            cardinality=Cardinality.MANY_TO_ONE,
            measure_names=["Revenue per Order"],
            component_measures=["Total Revenue", "Order Count"],
        )
        with pytest.raises(FanoutError, match="fanout"):
            detect_fanout(resolved, model)

    def test_no_join_steps_no_error(self) -> None:
        """No join steps means no fanout check needed."""
        model = _make_model()
        resolved = ResolvedQuery(
            measures=[
                ResolvedMeasure(
                    name="Total Revenue",
                    aggregation="sum",
                    expression=FunctionCall(
                        name="SUM",
                        args=[ColumnRef(name="AMOUNT", table="Orders")],
                    ),
                )
            ],
            base_object="Orders",
        )
        detect_fanout(resolved, model)  # should not raise


class TestJunctionTableFanout:
    """Fanout through junction/bridge tables is resolved by GROUP BY dimensions."""

    @staticmethod
    def _make_movies_model() -> SemanticModel:
        """Load the bundled movies model (Movies/Directors/Producers with
        many-to-many bridges Movie Directors and Movie Producers).

        The YAML lives in ``examples/movies.obml.yml`` so it can also be
        loaded into the UI for hand-testing junction-table fanout.
        """
        from pathlib import Path

        from orionbelt.parser.loader import TrackedLoader
        from orionbelt.parser.resolver import ReferenceResolver

        path = Path(__file__).resolve().parents[2] / "examples" / "movies.obml.yml"
        raw, src = TrackedLoader().load(path)
        model, _ = ReferenceResolver().resolve(raw, src)
        return model

    def test_junction_fanout_resolved_both_dims(self) -> None:
        """Director + Producer + Movies Cnt: fanout through both junctions
        is resolved because both dimensions are in GROUP BY.
        COUNT is additive → warnings emitted for each junction."""
        model = self._make_movies_model()
        resolved = ResolvedQuery(
            dimensions=[
                ResolvedDimension(
                    name="Director",
                    object_name="Directors",
                    column_name="Director Name",
                    source_column="name",
                ),
                ResolvedDimension(
                    name="Producer",
                    object_name="Producers",
                    column_name="Producer Name",
                    source_column="name",
                ),
            ],
            measures=[
                ResolvedMeasure(
                    name="Movies Cnt",
                    aggregation="count",
                    expression=FunctionCall(
                        name="COUNT",
                        args=[ColumnRef(name="movie_id", table="Movies")],
                    ),
                ),
            ],
            base_object="Movies",
            required_objects={
                "Movies",
                "Movie Directors",
                "Directors",
                "Movie Producers",
                "Producers",
            },
            join_steps=[
                # Movies ← Movie Directors (reversed many-to-one)
                JoinStep(
                    from_object="Movie Directors",
                    to_object="Movies",
                    from_columns=["Movie ID"],
                    to_columns=["Movie ID"],
                    join_type=ASTJoinType.LEFT,
                    cardinality=Cardinality.MANY_TO_ONE,
                    reversed=True,
                ),
                # Movie Directors → Directors (forward many-to-one)
                JoinStep(
                    from_object="Movie Directors",
                    to_object="Directors",
                    from_columns=["Director ID"],
                    to_columns=["Director ID"],
                    join_type=ASTJoinType.LEFT,
                    cardinality=Cardinality.MANY_TO_ONE,
                    reversed=False,
                ),
                # Movies ← Movie Producers (reversed many-to-one)
                JoinStep(
                    from_object="Movie Producers",
                    to_object="Movies",
                    from_columns=["Movie ID"],
                    to_columns=["Movie ID"],
                    join_type=ASTJoinType.LEFT,
                    cardinality=Cardinality.MANY_TO_ONE,
                    reversed=True,
                ),
                # Movie Producers → Producers (forward many-to-one)
                JoinStep(
                    from_object="Movie Producers",
                    to_object="Producers",
                    from_columns=["Producer ID"],
                    to_columns=["Producer ID"],
                    join_type=ASTJoinType.LEFT,
                    cardinality=Cardinality.MANY_TO_ONE,
                    reversed=False,
                ),
            ],
            measure_source_objects={"Movies"},
        )
        detect_fanout(resolved, model)  # should NOT raise
        # COUNT is additive — two junctions produce two warnings
        assert len(resolved.warnings) == 2
        assert all("cross-join" in w.message for w in resolved.warnings)
        assert all(w.code == "FAN_TRAP_RISK" for w in resolved.warnings)
        assert any("Movie Directors" in w.message for w in resolved.warnings)
        assert any("Movie Producers" in w.message for w in resolved.warnings)

    def test_junction_fanout_resolved_single_dim(self) -> None:
        """Director + Movies Cnt (no Producer): fanout through Movie Directors
        is resolved because Director is in GROUP BY."""
        model = self._make_movies_model()
        resolved = ResolvedQuery(
            dimensions=[
                ResolvedDimension(
                    name="Director",
                    object_name="Directors",
                    column_name="Director Name",
                    source_column="name",
                ),
            ],
            measures=[
                ResolvedMeasure(
                    name="Movies Cnt",
                    aggregation="count",
                    expression=FunctionCall(
                        name="COUNT",
                        args=[ColumnRef(name="movie_id", table="Movies")],
                    ),
                ),
            ],
            base_object="Movies",
            required_objects={"Movies", "Movie Directors", "Directors"},
            join_steps=[
                JoinStep(
                    from_object="Movie Directors",
                    to_object="Movies",
                    from_columns=["Movie ID"],
                    to_columns=["Movie ID"],
                    join_type=ASTJoinType.LEFT,
                    cardinality=Cardinality.MANY_TO_ONE,
                    reversed=True,
                ),
                JoinStep(
                    from_object="Movie Directors",
                    to_object="Directors",
                    from_columns=["Director ID"],
                    to_columns=["Director ID"],
                    join_type=ASTJoinType.LEFT,
                    cardinality=Cardinality.MANY_TO_ONE,
                    reversed=False,
                ),
            ],
            measure_source_objects={"Movies"},
        )
        detect_fanout(resolved, model)  # should NOT raise
        assert len(resolved.warnings) == 1
        assert "Movie Directors" in resolved.warnings[0].message

    def test_junction_no_warning_for_non_additive(self) -> None:
        """MIN/MAX aggregations don't produce inflated totals — no warning."""
        model = self._make_movies_model()
        # Override measure to use MAX
        model.measures["Movies Cnt"] = Measure(
            label="Movies Cnt",
            columns=[{"dataObject": "Movies", "column": "Movie ID"}],
            result_type=DataType.INT,
            aggregation="max",
        )
        resolved = ResolvedQuery(
            dimensions=[
                ResolvedDimension(
                    name="Director",
                    object_name="Directors",
                    column_name="Director Name",
                    source_column="name",
                ),
            ],
            measures=[
                ResolvedMeasure(
                    name="Movies Cnt",
                    aggregation="max",
                    expression=FunctionCall(
                        name="MAX",
                        args=[ColumnRef(name="movie_id", table="Movies")],
                    ),
                ),
            ],
            base_object="Movies",
            required_objects={"Movies", "Movie Directors", "Directors"},
            join_steps=[
                JoinStep(
                    from_object="Movie Directors",
                    to_object="Movies",
                    from_columns=["Movie ID"],
                    to_columns=["Movie ID"],
                    join_type=ASTJoinType.LEFT,
                    cardinality=Cardinality.MANY_TO_ONE,
                    reversed=True,
                ),
                JoinStep(
                    from_object="Movie Directors",
                    to_object="Directors",
                    from_columns=["Director ID"],
                    to_columns=["Director ID"],
                    join_type=ASTJoinType.LEFT,
                    cardinality=Cardinality.MANY_TO_ONE,
                    reversed=False,
                ),
            ],
            measure_source_objects={"Movies"},
        )
        detect_fanout(resolved, model)  # should NOT raise
        assert len(resolved.warnings) == 0  # MAX is not additive — no warning

    def test_pipeline_junction_fanout_compiles(self) -> None:
        """Full pipeline: Director + Producer + Movies Cnt should compile."""
        model = self._make_movies_model()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Director", "Producer"],
                measures=["Movies Cnt"],
            )
        )
        pipeline = CompilationPipeline()
        result = pipeline.compile(query, model, "postgres")
        assert "COUNT" in result.sql
        assert result.resolved.dimensions == ["Director", "Producer"]
        assert result.resolved.measures == ["Movies Cnt"]


# -- Pipeline integration test ----------------------------------------------


class TestPipelineFanout:
    def test_pipeline_raises_unreachable_error(self) -> None:
        """CompilationPipeline should refuse to compile when a required object
        is unreachable via directed joins.

        The join is declared as Orders many-to-one Customers.  The measure is
        on Customers (dimension table) and the dimension is on Orders (fact
        table).  Resolution selects Customers as the base object (measure
        source).  Reaching Orders from Customers would require walking the
        many-to-one in reverse, which would inflate row counts — so the
        resolver raises UNREACHABLE_REQUIRED_OBJECT instead of generating
        silently-wrong SQL.
        """
        orders = DataObject(
            label="Orders",
            code="ORDERS",
            database="WH",
            schema_name="PUBLIC",
            columns={
                "Order ID": DataObjectColumn(
                    label="Order ID", code="ORDER_ID", abstract_type=DataType.STRING
                ),
                "Customer ID": DataObjectColumn(
                    label="Customer ID", code="CUSTOMER_ID", abstract_type=DataType.STRING
                ),
            },
            joins=[
                DataObjectJoin(
                    join_type=Cardinality.MANY_TO_ONE,
                    join_to="Customers",
                    columns_from=["Customer ID"],
                    columns_to=["Cust ID"],
                )
            ],
        )
        customers = DataObject(
            label="Customers",
            code="CUSTOMERS",
            database="WH",
            schema_name="PUBLIC",
            columns={
                "Cust ID": DataObjectColumn(
                    label="Cust ID", code="CUST_ID", abstract_type=DataType.STRING
                ),
                "Revenue": DataObjectColumn(
                    label="Revenue", code="REVENUE", abstract_type=DataType.FLOAT
                ),
            },
        )

        model = SemanticModel(
            data_objects={"Orders": orders, "Customers": customers},
            dimensions={
                "Order ID": Dimension(
                    label="Order ID",
                    view="Orders",
                    column="Order ID",
                    result_type=DataType.STRING,
                ),
            },
            measures={
                "Cust Revenue": Measure(
                    label="Cust Revenue",
                    columns=[{"dataObject": "Customers", "column": "Revenue"}],
                    result_type=DataType.FLOAT,
                    aggregation="sum",
                ),
            },
        )

        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order ID"],
                measures=["Cust Revenue"],
            )
        )

        pipeline = CompilationPipeline()
        with pytest.raises(ResolutionError) as exc_info:
            pipeline.compile(query, model, "postgres")
        codes = {e.code for e in exc_info.value.errors}
        assert "UNREACHABLE_REQUIRED_OBJECT" in codes

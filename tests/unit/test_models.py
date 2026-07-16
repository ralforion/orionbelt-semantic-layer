"""Tests for Pydantic domain models."""

from __future__ import annotations

from orionbelt.models.errors import SemanticError, SourceSpan, ValidationResult
from orionbelt.models.query import (
    DimensionRef,
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
)
from orionbelt.models.semantic import (
    AggregationType,
    Cardinality,
    DataColumnRef,
    DataObject,
    DataObjectColumn,
    DataType,
    Dimension,
    JoinType,
    Measure,
    NumClass,
    TimeGrain,
)


class TestDataTypes:
    def test_data_type_values(self) -> None:
        assert DataType.STRING == "string"
        assert DataType.FLOAT == "float"
        assert DataType.TIMESTAMP_TZ == "timestamp_tz"

    def test_aggregation_type_values(self) -> None:
        assert AggregationType.SUM == "sum"
        assert AggregationType.COUNT_DISTINCT == "count_distinct"

    def test_join_type_values(self) -> None:
        assert JoinType.LEFT == "left"
        assert JoinType.INNER == "inner"

    def test_cardinality_values(self) -> None:
        assert Cardinality.MANY_TO_ONE == "many-to-one"
        assert Cardinality.ONE_TO_ONE == "one-to-one"

    def test_time_grain_values(self) -> None:
        assert TimeGrain.MONTH == "month"
        assert TimeGrain.QUARTER == "quarter"

    def test_num_class_values(self) -> None:
        assert NumClass.CATEGORICAL == "categorical"
        assert NumClass.ADDITIVE == "additive"
        assert NumClass.NON_ADDITIVE == "non-additive"


class TestDataColumnRef:
    def test_data_column_ref_with_data_object_and_column(self) -> None:
        ref = DataColumnRef(view="Sales", column="Amount")
        assert ref.view == "Sales"
        assert ref.column == "Amount"


class TestDataObjectColumn:
    def test_data_object_column_creation(self) -> None:
        col = DataObjectColumn(name="Amount", code="AMOUNT", abstractType="float")
        assert col.name == "Amount"
        assert col.code == "AMOUNT"
        assert col.abstract_type == DataType.FLOAT

    def test_data_object_column_with_num_class(self) -> None:
        col = DataObjectColumn(
            name="Amount", code="AMOUNT", abstractType="float", numClass="additive"
        )
        assert col.num_class == NumClass.ADDITIVE

    def test_data_object_column_num_class_defaults_to_none(self) -> None:
        col = DataObjectColumn(name="Amount", code="AMOUNT", abstractType="float")
        assert col.num_class is None


class TestDataObject:
    def test_data_object_qualified_code(self) -> None:
        obj = DataObject(
            name="Orders",
            code="ORDERS",
            database="WAREHOUSE",
            schema="PUBLIC",
        )
        assert obj.qualified_code == "WAREHOUSE.PUBLIC.ORDERS"


class TestDimension:
    def test_dimension_with_data_object_column(self) -> None:
        dim = Dimension(name="Country", view="Customers", column="Country", resultType="string")
        assert dim.view == "Customers"
        assert dim.column == "Country"


class TestMeasure:
    def test_simple_measure(self) -> None:
        m = Measure(
            name="Revenue",
            columns=[DataColumnRef(view="Orders", column="Amount")],
            resultType="float",
            aggregation="sum",
        )
        assert m.aggregation == "sum"
        assert len(m.columns) == 1

    def test_measure_with_expression(self) -> None:
        m = Measure(
            name="Profit",
            columns=[
                DataColumnRef(view="Sales", column="Amount"),
                DataColumnRef(view="Sales", column="Cost"),
            ],
            resultType="float",
            aggregation="sum",
            expression="{[Amount]} - {[Cost]}",
        )
        assert m.expression == "{[Amount]} - {[Cost]}"


class TestDimensionRef:
    def test_parse_simple(self) -> None:
        ref = DimensionRef.parse("Customer Country")
        assert ref.name == "Customer Country"
        assert ref.grain is None

    def test_parse_with_grain(self) -> None:
        ref = DimensionRef.parse("Order Date:month")
        assert ref.name == "Order Date"
        assert ref.grain == TimeGrain.MONTH


class TestQueryObject:
    def test_basic_query(self) -> None:
        q = QueryObject(
            select=QuerySelect(
                dimensions=["Customer Country"],
                measures=["Revenue"],
            ),
            where=[
                QueryFilter(field="Customer Segment", op=FilterOperator.IN, value=["SMB"]),
            ],
            limit=100,
        )
        assert len(q.select.dimensions) == 1
        assert len(q.select.measures) == 1
        assert q.limit == 100


class TestErrors:
    def test_source_span(self) -> None:
        span = SourceSpan(file="model.yaml", line=10, column=5)
        assert span.file == "model.yaml"
        assert span.line == 10

    def test_semantic_error_with_suggestions(self) -> None:
        err = SemanticError(
            code="UNKNOWN_DATA_OBJECT",
            message="Unknown data object 'Custmers'",
            suggestions=["Customers"],
        )
        assert err.suggestions == ["Customers"]

    def test_validation_result(self) -> None:
        result = ValidationResult(valid=True)
        assert result.valid
        assert result.errors == []

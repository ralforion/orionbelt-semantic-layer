"""Tests for the diagram generation service."""

from __future__ import annotations

from orionbelt.models.semantic import (
    Cardinality,
    DataObject,
    DataObjectColumn,
    DataObjectJoin,
    DataType,
    Dimension,
    SemanticModel,
)
from orionbelt.service.diagram import generate_mermaid_er


def _sample_model() -> SemanticModel:
    """Build a small model with two data objects and one join."""
    return SemanticModel(
        data_objects={
            "Orders": DataObject(
                label="Orders",
                code="orders",
                database="DB",
                schema_name="PUBLIC",
                columns={
                    "Order ID": DataObjectColumn(
                        label="Order ID", code="order_id", abstract_type=DataType.STRING
                    ),
                    "Customer ID": DataObjectColumn(
                        label="Customer ID", code="customer_id", abstract_type=DataType.STRING
                    ),
                    "Amount": DataObjectColumn(
                        label="Amount", code="amount", abstract_type=DataType.FLOAT
                    ),
                },
                joins=[
                    DataObjectJoin(
                        join_type=Cardinality.MANY_TO_ONE,
                        join_to="Customers",
                        columns_from=["Customer ID"],
                        columns_to=["Cust ID"],
                    ),
                ],
            ),
            "Customers": DataObject(
                label="Customers",
                code="customers",
                database="DB",
                schema_name="PUBLIC",
                columns={
                    "Cust ID": DataObjectColumn(
                        label="Cust ID", code="cust_id", abstract_type=DataType.STRING
                    ),
                    "Name": DataObjectColumn(
                        label="Name", code="name", abstract_type=DataType.STRING
                    ),
                },
            ),
        },
        dimensions={
            "Customer Name": Dimension(
                label="Customer Name",
                view="Customers",
                column="Name",
                result_type=DataType.STRING,
            ),
        },
        measures={},
    )


class TestGenerateMermaidER:
    def test_basic_output_contains_er_diagram(self) -> None:
        model = _sample_model()
        result = generate_mermaid_er(model)
        assert "erDiagram" in result

    def test_direction_lr(self) -> None:
        model = _sample_model()
        result = generate_mermaid_er(model)
        assert "direction LR" in result

    def test_default_theme(self) -> None:
        model = _sample_model()
        result = generate_mermaid_er(model)
        assert "'theme': 'default'" in result

    def test_custom_theme(self) -> None:
        model = _sample_model()
        result = generate_mermaid_er(model, theme="dark")
        assert "'theme': 'dark'" in result

    def test_entities_present(self) -> None:
        model = _sample_model()
        result = generate_mermaid_er(model)
        assert "Orders {" in result
        assert "Customers {" in result

    def test_columns_present(self) -> None:
        # Attribute identifiers use the column's physical `code` (which is
        # space-free by definition) — Mermaid's ER grammar disallows
        # spaces in attribute names, and the physical name is the most
        # honest thing to render.
        model = _sample_model()
        result = generate_mermaid_er(model)
        assert "string order_id" in result
        assert "float amount" in result
        assert "string cust_id" in result

    def test_fk_annotation(self) -> None:
        model = _sample_model()
        result = generate_mermaid_er(model)
        assert "string customer_id FK" in result
        # Non-FK columns should NOT have FK marker
        assert "string order_id FK" not in result

    def test_pk_annotation(self) -> None:
        model = _sample_model()
        # Mark Order ID as primary key on Orders, Cust ID on Customers
        model.data_objects["Orders"].columns["Order ID"].primary_key = True
        model.data_objects["Customers"].columns["Cust ID"].primary_key = True
        result = generate_mermaid_er(model)
        assert "string order_id PK" in result
        assert "string cust_id PK" in result
        # Plain columns stay unmarked
        assert "float amount PK" not in result
        assert "float amount FK" not in result

    def test_pk_takes_precedence_over_fk(self) -> None:
        # When a column is both PK (declared) and FK (used in join), PK wins.
        model = _sample_model()
        model.data_objects["Orders"].columns["Customer ID"].primary_key = True
        result = generate_mermaid_er(model)
        assert "string customer_id PK" in result
        assert "string customer_id FK" not in result

    def test_relationship_present(self) -> None:
        model = _sample_model()
        result = generate_mermaid_er(model)
        # many-to-one: }o--||  — and the join label preserves the
        # original spaced business label rather than munging it.
        assert '}o--|| Customers : "Customer ID"' in result

    def test_show_columns_false(self) -> None:
        model = _sample_model()
        result = generate_mermaid_er(model, show_columns=False)
        assert "erDiagram" in result
        # Entities should be listed but without { }
        assert "Orders {" not in result
        assert "Customers {" not in result
        # Relationships should still be there
        assert "Customers" in result

    def test_empty_model(self) -> None:
        model = SemanticModel()
        result = generate_mermaid_er(model)
        assert "erDiagram" in result

    def test_secondary_join_dotted_line(self) -> None:
        model = SemanticModel(
            data_objects={
                "Sales": DataObject(
                    label="Sales",
                    code="sales",
                    database="DB",
                    schema_name="PUBLIC",
                    columns={
                        "Date": DataObjectColumn(
                            label="Date", code="dt", abstract_type=DataType.DATE
                        ),
                    },
                    joins=[
                        DataObjectJoin(
                            join_type=Cardinality.MANY_TO_ONE,
                            join_to="Calendar",
                            columns_from=["Date"],
                            columns_to=["Cal Date"],
                            secondary=True,
                            path_name="sales_date",
                        ),
                    ],
                ),
                "Calendar": DataObject(
                    label="Calendar",
                    code="calendar",
                    database="DB",
                    schema_name="PUBLIC",
                    columns={
                        "Cal Date": DataObjectColumn(
                            label="Cal Date", code="cal_date", abstract_type=DataType.DATE
                        ),
                    },
                ),
            },
        )
        result = generate_mermaid_er(model)
        # Secondary uses dotted line (..) and path_name as label
        assert '}o..|| Calendar : "sales_date"' in result

    def test_one_to_one_cardinality(self) -> None:
        model = SemanticModel(
            data_objects={
                "A": DataObject(
                    label="A",
                    code="a",
                    database="DB",
                    schema_name="PUBLIC",
                    joins=[
                        DataObjectJoin(
                            join_type=Cardinality.ONE_TO_ONE,
                            join_to="B",
                            columns_from=["id"],
                            columns_to=["id"],
                        ),
                    ],
                ),
                "B": DataObject(label="B", code="b", database="DB", schema_name="PUBLIC"),
            },
        )
        result = generate_mermaid_er(model)
        assert "||--||" in result

    def test_quotes_entity_names_with_spaces(self) -> None:
        # Entity names containing spaces are rendered double-quoted so
        # Mermaid accepts them verbatim (no underscore munging). Attribute
        # identifiers use the column's `code`, which is space-free.
        model = SemanticModel(
            data_objects={
                "Account Balances": DataObject(
                    label="Account Balances",
                    code="acct_bal",
                    database="DB",
                    schema_name="PUBLIC",
                    columns={
                        "Account ID": DataObjectColumn(
                            label="Account ID",
                            code="account_id",
                            abstract_type=DataType.STRING,
                        ),
                    },
                ),
            },
        )
        result = generate_mermaid_er(model)
        assert '"Account Balances" {' in result
        assert "string account_id" in result

    def test_with_sales_fixture(self, sales_model: SemanticModel) -> None:
        """Integration test with the full sales model fixture."""
        result = generate_mermaid_er(sales_model)
        assert "erDiagram" in result
        # Check known entities from the fixture
        assert "Orders {" in result
        assert "Customers {" in result
        assert "Products {" in result
        # Check a known relationship
        assert "}o--|| Customers" in result

"""Tests for YAML parser, resolver, and validator."""

from __future__ import annotations

from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator
from tests.conftest import SALES_MODEL_DIR, SAMPLE_MODEL_YAML


class TestTrackedLoader:
    def test_load_string(self, loader: TrackedLoader) -> None:
        raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
        assert "dataObjects" in raw
        assert "dimensions" in raw
        assert "measures" in raw
        assert raw["version"] == 1.0

    def test_load_string_empty(self, loader: TrackedLoader) -> None:
        raw, source_map = loader.load_string("")
        assert raw == {}

    def test_source_map_has_positions(self, loader: TrackedLoader) -> None:
        raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
        # Should have position info for dataObjects, dimensions, measures
        assert len(source_map.paths) > 0

    def test_load_model_file(self, loader: TrackedLoader) -> None:
        raw, source_map = loader.load(SALES_MODEL_DIR / "model.yaml")
        assert "dataObjects" in raw
        assert "Orders" in raw["dataObjects"]
        assert "Customers" in raw["dataObjects"]

    def test_data_objects_have_columns(self, loader: TrackedLoader) -> None:
        raw, _ = loader.load_string(SAMPLE_MODEL_YAML)
        orders = raw["dataObjects"]["Orders"]
        assert "Order ID" in orders["columns"]
        assert orders["columns"]["Amount"]["abstractType"] == "float"


class TestReferenceResolver:
    def test_resolve_valid_model(self, resolver: ReferenceResolver) -> None:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid
        assert len(model.data_objects) == 2
        assert len(model.dimensions) == 1
        assert len(model.measures) == 3

    def test_resolve_dimension_references(self, resolver: ReferenceResolver) -> None:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        dim = model.dimensions["Customer Country"]
        assert dim.view == "Customers"
        assert dim.column == "Country"

    def test_unknown_data_object_error(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: DB
    schema: SCH
    columns:
      ID:
        code: ID
        abstractType: string
dimensions:
  Bad Dim:
    dataObject: NonExistent
    column: Foo
    resultType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        assert not result.valid
        assert any(e.code == "UNKNOWN_DATA_OBJECT" for e in result.errors)

    def test_unknown_column_error(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: DB
    schema: SCH
    columns:
      ID:
        code: ID
        abstractType: string
dimensions:
  Bad Dim:
    dataObject: Orders
    column: NonExistent
    resultType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        assert not result.valid
        assert any(e.code == "UNKNOWN_COLUMN" for e in result.errors)

    def test_resolve_sales_model(self) -> None:
        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load(SALES_MODEL_DIR / "model.yaml")
        model, result = resolver.resolve(raw, source_map)
        assert result.valid, f"Errors: {[e.message for e in result.errors]}"
        assert "Orders" in model.data_objects
        assert "Revenue" in model.measures
        assert "Customer Country" in model.dimensions

    def test_resolve_dimension_data_object(self) -> None:
        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load(SALES_MODEL_DIR / "model.yaml")
        model, result = resolver.resolve(raw, source_map)
        # Product Category uses dataObject + field
        assert "Product Category" in model.dimensions
        dim = model.dimensions["Product Category"]
        assert dim.view == "Products"
        assert dim.column == "Category"


class TestSemanticValidator:
    def test_valid_model(self, sales_model) -> None:
        validator = SemanticValidator()
        errors = validator.validate(sales_model)
        assert len(errors) == 0

    def test_dimension_may_share_name_with_data_object(self, resolver: ReferenceResolver) -> None:
        """Dimension names can match data object names (different namespaces)."""
        yaml_content = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: DB
    schema: SCH
    columns:
      id:
        code: ID
        abstractType: string
dimensions:
  Orders:
    dataObject: Orders
    column: id
    resultType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert not any(e.code == "DUPLICATE_IDENTIFIER" for e in errors)

    def test_duplicate_identifier_dimension_measure(self, resolver: ReferenceResolver) -> None:
        """Dimension and measure with the same name should still error."""
        yaml_content = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: DB
    schema: SCH
    columns:
      id:
        code: ID
        abstractType: string
      amt:
        code: AMT
        abstractType: float
        numClass: additive
dimensions:
  Revenue:
    dataObject: Orders
    column: id
    resultType: string
measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: amt
    resultType: float
    aggregation: sum
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert any(e.code == "DUPLICATE_IDENTIFIER" for e in errors)

    def test_cyclic_join_detection(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      id:
        code: ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [id]
        columnsTo: [id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      id:
        code: ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: A
        columnsFrom: [id]
        columnsTo: [id]
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert any(e.code == "CYCLIC_JOIN" for e in errors)

    def test_unknown_join_target(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      id:
        code: ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: NonExistent
        columnsFrom: [id]
        columnsTo: [id]
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert any(e.code == "UNKNOWN_JOIN_TARGET" for e in errors)

    def test_join_column_count_mismatch(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      id1:
        code: ID1
        abstractType: string
      id2:
        code: ID2
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [id1, id2]
        columnsTo: [id1]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      id1:
        code: ID1
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert any(e.code == "JOIN_COLUMN_COUNT_MISMATCH" for e in errors)

    def test_multipath_join_detection(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: D
        columnsFrom: [a_id]
        columnsTo: [d_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: C
        columnsFrom: [b_id]
        columnsTo: [c_id]
  C:
    code: C
    database: DB
    schema: SCH
    columns:
      c_id:
        code: C_ID
        abstractType: string
  D:
    code: D
    database: DB
    schema: SCH
    columns:
      d_id:
        code: D_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: C
        columnsFrom: [d_id]
        columnsTo: [c_id]
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        multipath_errors = [e for e in errors if e.code == "MULTIPATH_JOIN"]
        assert len(multipath_errors) == 1
        assert "A" in multipath_errors[0].message
        assert "C" in multipath_errors[0].message

    def test_no_multipath_in_tree(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: C
        columnsFrom: [a_id]
        columnsTo: [c_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
  C:
    code: C
    database: DB
    schema: SCH
    columns:
      c_id:
        code: C_ID
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert not any(e.code == "MULTIPATH_JOIN" for e in errors)

    def test_multipath_longer_paths(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: E
        columnsFrom: [a_id]
        columnsTo: [e_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: C
        columnsFrom: [b_id]
        columnsTo: [c_id]
  C:
    code: C
    database: DB
    schema: SCH
    columns:
      c_id:
        code: C_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: D
        columnsFrom: [c_id]
        columnsTo: [d_id]
  D:
    code: D
    database: DB
    schema: SCH
    columns:
      d_id:
        code: D_ID
        abstractType: string
  E:
    code: E
    database: DB
    schema: SCH
    columns:
      e_id:
        code: E_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: D
        columnsFrom: [e_id]
        columnsTo: [d_id]
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        multipath_errors = [e for e in errors if e.code == "MULTIPATH_JOIN"]
        assert len(multipath_errors) == 1
        assert "A" in multipath_errors[0].message
        assert "D" in multipath_errors[0].message

    def test_no_multipath_direct_plus_indirect(self, resolver: ReferenceResolver) -> None:
        """Direct join + indirect path is valid snowflake — not ambiguous."""
        yaml_content = """\
version: 1.0
dataObjects:
  Purchases:
    code: purchases
    database: DB
    schema: SCH
    columns:
      purchase_id:
        code: purchase_id
        abstractType: string
      purchase_product:
        code: purchase_product
        abstractType: string
      purchase_supplier:
        code: purchase_supplier
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [purchase_product]
        columnsTo: [product_id]
      - joinType: many-to-one
        joinTo: Suppliers
        columnsFrom: [purchase_supplier]
        columnsTo: [supplier_id]
  Products:
    code: products
    database: DB
    schema: SCH
    columns:
      product_id:
        code: product_id
        abstractType: string
      product_supplier:
        code: product_supplier
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Suppliers
        columnsFrom: [product_supplier]
        columnsTo: [supplier_id]
  Suppliers:
    code: suppliers
    database: DB
    schema: SCH
    columns:
      supplier_id:
        code: supplier_id
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert not any(e.code == "MULTIPATH_JOIN" for e in errors)


class TestCustomExtensions:
    """Tests for customExtensions parsing at all 6 levels."""

    EXTENSIONS_MODEL_YAML = """\
version: 1.0
customExtensions:
  - vendor: GOVERNANCE
    data: '{"owner": "data-team"}'
dataObjects:
  Orders:
    code: ORDERS
    database: DB
    schema: SCH
    columns:
      Amount:
        code: AMOUNT
        abstractType: float
        customExtensions:
          - vendor: OSI
            data: '{"synonyms": ["revenue"]}'
      Order ID:
        code: ORDER_ID
        abstractType: string
    customExtensions:
      - vendor: OSI
        data: '{"instructions": "Main fact table"}'
dimensions:
  Order Amount:
    dataObject: Orders
    column: Amount
    resultType: float
    customExtensions:
      - vendor: OSI
        data: '{"examples": ["100.0"]}'
measures:
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
    customExtensions:
      - vendor: LINEAGE
        data: '{"source": "ERP"}'
metrics:
  Revenue Doubled:
    expression: '{[Total Revenue]} * 2'
    customExtensions:
      - vendor: GOVERNANCE
        data: '{"classification": "internal"}'
"""

    def test_model_level_extensions(self, resolver: ReferenceResolver) -> None:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(self.EXTENSIONS_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid, f"Errors: {[e.message for e in result.errors]}"
        assert len(model.custom_extensions) == 1
        assert model.custom_extensions[0].vendor == "GOVERNANCE"
        assert '"owner"' in model.custom_extensions[0].data

    def test_data_object_level_extensions(self, resolver: ReferenceResolver) -> None:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(self.EXTENSIONS_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid
        exts = model.data_objects["Orders"].custom_extensions
        assert len(exts) == 1
        assert exts[0].vendor == "OSI"
        assert '"instructions"' in exts[0].data

    def test_column_level_extensions(self, resolver: ReferenceResolver) -> None:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(self.EXTENSIONS_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid
        exts = model.data_objects["Orders"].columns["Amount"].custom_extensions
        assert len(exts) == 1
        assert exts[0].vendor == "OSI"
        assert '"synonyms"' in exts[0].data

    def test_dimension_level_extensions(self, resolver: ReferenceResolver) -> None:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(self.EXTENSIONS_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid
        exts = model.dimensions["Order Amount"].custom_extensions
        assert len(exts) == 1
        assert exts[0].vendor == "OSI"

    def test_measure_level_extensions(self, resolver: ReferenceResolver) -> None:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(self.EXTENSIONS_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid
        exts = model.measures["Total Revenue"].custom_extensions
        assert len(exts) == 1
        assert exts[0].vendor == "LINEAGE"

    def test_metric_level_extensions(self, resolver: ReferenceResolver) -> None:
        loader = TrackedLoader()
        raw, source_map = loader.load_string(self.EXTENSIONS_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid
        exts = model.metrics["Revenue Doubled"].custom_extensions
        assert len(exts) == 1
        assert exts[0].vendor == "GOVERNANCE"

    def test_empty_extensions_default(self, resolver: ReferenceResolver) -> None:
        """Model without customExtensions should have empty lists."""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(SAMPLE_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid
        assert model.custom_extensions == []
        assert model.data_objects["Orders"].custom_extensions == []
        assert model.data_objects["Orders"].columns["Amount"].custom_extensions == []
        assert model.dimensions["Customer Country"].custom_extensions == []
        assert model.measures["Total Revenue"].custom_extensions == []

    def test_extensions_do_not_affect_compilation(self) -> None:
        """Model with customExtensions should compile normally."""
        from orionbelt.compiler.pipeline import CompilationPipeline
        from orionbelt.models.query import QueryObject, QuerySelect

        loader = TrackedLoader()
        resolver = ReferenceResolver()
        raw, source_map = loader.load_string(self.EXTENSIONS_MODEL_YAML)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid

        pipeline = CompilationPipeline()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Order Amount"],
                measures=["Total Revenue"],
            ),
        )
        compile_result = pipeline.compile(query, model, "postgres")
        assert "SELECT" in compile_result.sql


class TestSecondaryJoinValidation:
    """Tests for secondary join validation rules."""

    def test_secondary_join_without_path_name_errors(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
      a_alt:
        code: A_ALT
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: B
        secondary: true
        columnsFrom: [a_alt]
        columnsTo: [b_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert any(e.code == "SECONDARY_JOIN_MISSING_PATH_NAME" for e in errors)

    def test_secondary_join_with_path_name_ok(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
      a_alt:
        code: A_ALT
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: B
        secondary: true
        pathName: alt_path
        columnsFrom: [a_alt]
        columnsTo: [b_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert not any(e.code == "SECONDARY_JOIN_MISSING_PATH_NAME" for e in errors)

    def test_duplicate_path_name_for_same_pair_errors(self, resolver: ReferenceResolver) -> None:
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
      a_alt1:
        code: A_ALT1
        abstractType: string
      a_alt2:
        code: A_ALT2
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: B
        secondary: true
        pathName: dup_path
        columnsFrom: [a_alt1]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: B
        secondary: true
        pathName: dup_path
        columnsFrom: [a_alt2]
        columnsTo: [b_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert any(e.code == "DUPLICATE_JOIN_PATH_NAME" for e in errors)

    def test_same_path_name_different_pairs_ok(self, resolver: ReferenceResolver) -> None:
        """Same pathName on different (source, target) pairs is allowed."""
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
      a_alt:
        code: A_ALT
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: B
        secondary: true
        pathName: alt
        columnsFrom: [a_alt]
        columnsTo: [b_id]
  X:
    code: X
    database: DB
    schema: SCH
    columns:
      x_id:
        code: X_ID
        abstractType: string
      x_alt:
        code: X_ALT
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Y
        columnsFrom: [x_id]
        columnsTo: [y_id]
      - joinType: many-to-one
        joinTo: Y
        secondary: true
        pathName: alt
        columnsFrom: [x_alt]
        columnsTo: [y_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
  Y:
    code: Y
    database: DB
    schema: SCH
    columns:
      y_id:
        code: Y_ID
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert not any(e.code == "DUPLICATE_JOIN_PATH_NAME" for e in errors)

    def test_secondary_joins_excluded_from_cycle_detection(
        self, resolver: ReferenceResolver
    ) -> None:
        """A secondary join that would create a cycle should NOT be flagged."""
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
      a_back:
        code: A_BACK
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: B
        secondary: true
        pathName: back_path
        columnsFrom: [a_back]
        columnsTo: [b_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: A
        columnsFrom: [b_id]
        columnsTo: [a_id]
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        # The primary A→B + B→A creates a cycle, but the secondary should not add more
        cycle_errors = [e for e in errors if e.code == "CYCLIC_JOIN"]
        assert len(cycle_errors) == 1  # only the primary cycle

    def test_secondary_joins_excluded_from_multipath_detection(
        self, resolver: ReferenceResolver
    ) -> None:
        """Secondary joins should not trigger multipath errors."""
        yaml_content = """\
version: 1.0
dataObjects:
  Flights:
    code: flights
    database: DB
    schema: SCH
    columns:
      flight_id:
        code: FLIGHT_ID
        abstractType: string
      dep_airport:
        code: DEP_AIRPORT
        abstractType: string
      arr_airport:
        code: ARR_AIRPORT
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Airports
        columnsFrom: [dep_airport]
        columnsTo: [airport_id]
      - joinType: many-to-one
        joinTo: Airports
        secondary: true
        pathName: arrival
        columnsFrom: [arr_airport]
        columnsTo: [airport_id]
  Airports:
    code: airports
    database: DB
    schema: SCH
    columns:
      airport_id:
        code: AIRPORT_ID
        abstractType: string
      airport_name:
        code: AIRPORT_NAME
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert not any(e.code == "MULTIPATH_JOIN" for e in errors)

    def test_parse_secondary_join_fields(self, resolver: ReferenceResolver) -> None:
        """Verify secondary and pathName are parsed correctly."""
        yaml_content = """\
version: 1.0
dataObjects:
  A:
    code: A
    database: DB
    schema: SCH
    columns:
      a_id:
        code: A_ID
        abstractType: string
      a_alt:
        code: A_ALT
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: B
        columnsFrom: [a_id]
        columnsTo: [b_id]
      - joinType: many-to-one
        joinTo: B
        secondary: true
        pathName: alt_path
        columnsFrom: [a_alt]
        columnsTo: [b_id]
  B:
    code: B
    database: DB
    schema: SCH
    columns:
      b_id:
        code: B_ID
        abstractType: string
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        assert result.valid
        joins = model.data_objects["A"].joins
        assert len(joins) == 2
        assert joins[0].secondary is False
        assert joins[0].path_name is None
        assert joins[1].secondary is True
        assert joins[1].path_name == "alt_path"

    def test_num_class_on_non_numeric_column(self, resolver: ReferenceResolver) -> None:
        """numClass on a string column should produce NUM_CLASS_ON_NON_NUMERIC."""
        yaml_content = """\
version: 1.0
dataObjects:
  T:
    code: T
    database: DB
    schema: SCH
    columns:
      Name:
        code: NAME
        abstractType: string
        numClass: additive
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert any(e.code == "NUM_CLASS_ON_NON_NUMERIC" for e in errors)

    def test_num_class_on_numeric_column_ok(self, resolver: ReferenceResolver) -> None:
        """numClass on int/float columns should not produce errors."""
        yaml_content = """\
version: 1.0
dataObjects:
  T:
    code: T
    database: DB
    schema: SCH
    columns:
      Qty:
        code: QTY
        abstractType: int
        numClass: additive
      Price:
        code: PRICE
        abstractType: float
        numClass: non-additive
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml_content)
        model, result = resolver.resolve(raw, source_map)
        validator = SemanticValidator()
        errors = validator.validate(model)
        assert not any(e.code == "NUM_CLASS_ON_NON_NUMERIC" for e in errors)

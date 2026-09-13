---
description: End-to-end OBML example for a sales analytics model with customers, products, and orders. Walks through dimensions, measures, metrics, joins, and compiled SQL.
---

# Sales Model Walkthrough

This example walks through a complete semantic model for a sales analytics use case with customers, products, and orders.

## The Model

```yaml
# yaml-language-server: $schema=schema/obml-schema.json
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Customer Name:
        code: NAME
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string
      Segment:
        code: SEGMENT
        abstractType: string

  Products:
    code: PRODUCTS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Product ID:
        code: PRODUCT_ID
        abstractType: string
      Product Name:
        code: NAME
        abstractType: string
      Category:
        code: CATEGORY
        abstractType: string

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Order Date:
        code: ORDER_DATE
        abstractType: date
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Product ID:
        code: PRODUCT_ID
        abstractType: string
      Quantity:
        code: QUANTITY
        abstractType: int
        numClass: additive
      Price:
        code: PRICE
        abstractType: float
        numClass: non-additive
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Customer ID
        columnsTo:
          - Customer ID
      - joinType: many-to-one
        joinTo: Products
        columnsFrom:
          - Product ID
        columnsTo:
          - Product ID

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string

  Customer Segment:
    dataObject: Customers
    column: Segment
    resultType: string

  Product Name:
    dataObject: Products
    column: Product Name
    resultType: string

  Product Category:
    dataObject: Products
    column: Category
    resultType: string

  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month

measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'

  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count

  Average Order Value:
    resultType: float
    aggregation: avg
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'

metrics:
  Revenue per Order:
    expression: '{[Revenue]} / {[Order Count]}'
```

## Data Objects Explained

### Star Schema Structure

This model follows a classic star schema:

```mermaid
graph LR
  O["<b>Orders</b><br/><i>fact</i><br/>Order ID<br/>Order Date<br/>Customer ID<br/>Product ID<br/>Quantity<br/>Price"]
  C["<b>Customers</b><br/>Customer ID<br/>Name<br/>Country<br/>Segment"]
  P["<b>Products</b><br/>Product ID<br/>Name<br/>Category"]

  O -->|many-to-one| C
  O -->|many-to-one| P
```

**Orders** is the fact table — it declares joins to both dimension tables. The compiler automatically identifies it as the base object because it has join definitions.

### Column Mapping

Each column maps a business name to a physical column:

| Data Object | Business Name | Physical Column |
|-------------|--------------|-----------------|
| Customers | Customer ID | `CUSTOMER_ID` |
| Customers | Country | `COUNTRY` |
| Products | Product Name | `NAME` |
| Orders | Price | `PRICE` |

The physical column name appears in generated SQL, while the business name is used in dimensions, measures, and queries.

## Dimensions Explained

### Standard Dimension

```yaml
Customer Country:
  dataObject: Customers
  column: Country
  resultType: string
```

References the `Country` column in the `Customers` data object. When queried, generates `"Customers"."COUNTRY"` with a GROUP BY.

### Time Dimension

```yaml
Order Date:
  dataObject: Orders
  column: Order Date
  resultType: date
  timeGrain: month
```

The `timeGrain: month` means queries using this dimension will apply `date_trunc('month', ...)` by default.

## Measures Explained

### Expression Measure — Revenue

```yaml
Revenue:
  resultType: float
  aggregation: sum
  expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'
```

Compiles to: `SUM("Orders"."PRICE" * "Orders"."QUANTITY")`

The expression references columns using `{[DataObject].[Column]}` syntax. The `sum` aggregation wraps the entire expression.

### Simple Measure — Order Count

```yaml
Order Count:
  columns:
    - dataObject: Orders
      column: Order ID
  resultType: int
  aggregation: count
```

Compiles to: `COUNT("Orders"."ORDER_ID")`

### Expression Measure — Average Order Value

```yaml
Average Order Value:
  resultType: float
  aggregation: avg
  expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'
```

Compiles to: `AVG("Orders"."PRICE" * "Orders"."QUANTITY")`

Uses `{[DataObject].[Column]}` syntax to reference columns directly in the expression — no `columns` array needed.

### Linking Measures to a Business Ontology

A measure can say what it *means* in a governed vocabulary with [external concept mappings](../guide/concept-mappings.md). Declare the prefixes once at the top level, then link the artefacts that carry business identity:

```yaml
ontology:
  prefixes:
    corp: "https://ontology.example.com/business/"
    schema: "https://schema.org/"

dataObjects:
  Customers:
    externalConceptMappings:
      - concept: schema:Organization
        relation: close

measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'
    externalConceptMappings:
      - concept: corp:GrossRevenue
        relation: exact
        justification: curated
        source: finance-glossary
        ontologyVersion: "2026.1"
      - concept: corp:Revenue        # the glossary's umbrella term
        relation: broader            # corp:Revenue is broader than this measure
```

Compiles to exactly the same SQL as before: mappings are metadata, not logic. They surface on `GET /v1/measures/Revenue` under `external_concept_mappings`, as `skos:exactMatch` / `skos:broadMatch` triples in the [RDF graph](../guide/obsl.md#external-concept-mappings-in-the-graph), and `GET /v1/concept-mappings/unmapped` lists what is still unlinked.

## Metrics Explained

```yaml
Revenue per Order:
  expression: '{[Revenue]} / {[Order Count]}'
```

This divides Revenue by Order Count. The `{[Revenue]}` and `{[Order Count]}` placeholders reference the measures defined above by name. Each measure is computed independently and then combined in the metric expression.

## Example Queries

### Revenue by Country

```python
query = QueryObject(
    select=QuerySelect(
        dimensions=["Customer Country"],
        measures=["Revenue", "Order Count"],
    ),
    order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
    limit=1000,
)
```

**Generated SQL (Postgres):**

```sql
SELECT
  "Customers"."COUNTRY" AS "Customer Country",
  SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue",
  COUNT("Orders"."ORDER_ID") AS "Order Count"
FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
  ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
GROUP BY "Customers"."COUNTRY"
ORDER BY "Revenue" DESC
LIMIT 1000
```

### Filtered Query — SMB Customers

```python
query = QueryObject(
    select=QuerySelect(
        dimensions=["Customer Country"],
        measures=["Revenue"],
    ),
    where=[
        QueryFilter(field="Customer Segment", op=FilterOperator.IN, value=["SMB", "MidMarket"]),
    ],
)
```

**Generated SQL:**

```sql
SELECT
  "Customers"."COUNTRY" AS "Customer Country",
  SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
  ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
WHERE ("Customers"."SEGMENT" IN ('SMB', 'MidMarket'))
GROUP BY "Customers"."COUNTRY"
```

### Multi-Dimension Query

```python
query = QueryObject(
    select=QuerySelect(
        dimensions=["Customer Country", "Product Category"],
        measures=["Revenue"],
    ),
)
```

**Generated SQL:**

```sql
SELECT
  "Customers"."COUNTRY" AS "Customer Country",
  "Products"."CATEGORY" AS "Product Category",
  SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
  ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
LEFT JOIN WAREHOUSE.PUBLIC.PRODUCTS AS "Products"
  ON "Orders"."PRODUCT_ID" = "Products"."PRODUCT_ID"
GROUP BY "Customers"."COUNTRY", "Products"."CATEGORY"
```

Notice how the compiler automatically adds the `Products` join because the `Product Category` dimension requires it.

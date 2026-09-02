-- Q85 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

WITH "__ob_main" AS (
  SELECT
    SUBSTRING(toString("Reason"."r_reason_desc"), 1, 20) AS "Reason Description 20",
    CAST(round(toDecimal256(toString(AVG("Web Sales"."ws_quantity")), 7), 6) AS Nullable(Decimal(18, 6))) AS "Avg Web Quantity"
  FROM "tpcds"."web_sales" AS "Web Sales"
  LEFT JOIN "tpcds"."web_returns" AS "Web Returns"
    ON "Web Sales"."ws_item_sk" = "Web Returns"."wr_item_sk"
    AND "Web Sales"."ws_order_number" = "Web Returns"."wr_order_number"
  LEFT JOIN "tpcds"."reason" AS "Reason"
    ON "Web Returns"."wr_reason_sk" = "Reason"."r_reason_sk"
  LEFT JOIN "tpcds"."date_dim" AS "Web Sold Date"
    ON "Web Sales"."ws_sold_date_sk" = "Web Sold Date"."d_date_sk"
  LEFT JOIN "tpcds"."customer_demographics" AS "Refunded Demographics"
    ON "Web Returns"."wr_refunded_cdemo_sk" = "Refunded Demographics"."cd_demo_sk"
  LEFT JOIN "tpcds"."customer_demographics" AS "Returning Demographics"
    ON "Web Returns"."wr_returning_cdemo_sk" = "Returning Demographics"."cd_demo_sk"
  LEFT JOIN "tpcds"."customer_address" AS "Refunded Address"
    ON "Web Returns"."wr_refunded_addr_sk" = "Refunded Address"."ca_address_sk"
  WHERE
    "Web Sold Date"."d_year" = 2000
    AND (
      "Refunded Demographics"."cd_marital_status" = 'M'
      AND (
        "Refunded Demographics"."cd_marital_status" = "Returning Demographics"."cd_marital_status"
      ) = TRUE
      AND "Refunded Demographics"."cd_education_status" = 'Advanced Degree'
      AND (
        "Refunded Demographics"."cd_education_status" = "Returning Demographics"."cd_education_status"
      ) = TRUE
      AND "Web Sales"."ws_sales_price" BETWEEN 100.0 AND 150.0
      OR "Refunded Demographics"."cd_marital_status" = 'S'
      AND (
        "Refunded Demographics"."cd_marital_status" = "Returning Demographics"."cd_marital_status"
      ) = TRUE
      AND "Refunded Demographics"."cd_education_status" = 'College'
      AND (
        "Refunded Demographics"."cd_education_status" = "Returning Demographics"."cd_education_status"
      ) = TRUE
      AND "Web Sales"."ws_sales_price" BETWEEN 50.0 AND 100.0
      OR "Refunded Demographics"."cd_marital_status" = 'W'
      AND (
        "Refunded Demographics"."cd_marital_status" = "Returning Demographics"."cd_marital_status"
      ) = TRUE
      AND "Refunded Demographics"."cd_education_status" = '2 yr Degree'
      AND (
        "Refunded Demographics"."cd_education_status" = "Returning Demographics"."cd_education_status"
      ) = TRUE
      AND "Web Sales"."ws_sales_price" BETWEEN 150.0 AND 200.0
    )
    AND (
      "Refunded Address"."ca_country" = 'United States'
      AND "Refunded Address"."ca_state" IN ('IN', 'OH', 'NJ')
      AND "Web Sales"."ws_net_profit" BETWEEN 100 AND 200
      OR "Refunded Address"."ca_country" = 'United States'
      AND "Refunded Address"."ca_state" IN ('WI', 'CT', 'KY')
      AND "Web Sales"."ws_net_profit" BETWEEN 150 AND 300
      OR "Refunded Address"."ca_country" = 'United States'
      AND "Refunded Address"."ca_state" IN ('LA', 'IA', 'AR')
      AND "Web Sales"."ws_net_profit" BETWEEN 50 AND 250
    )
  GROUP BY ALL
), "__ob_dedup_0" AS (
  SELECT
    "__ob_dedup_src_0"."Reason Description 20" AS "Reason Description 20",
    CAST(round(toDecimal256(toString(AVG("__ob_dedup_src_0"."__ob_c0")), 7), 6) AS Nullable(Decimal(18, 6))) AS "Avg Refunded Cash",
    CAST(round(toDecimal256(toString(AVG("__ob_dedup_src_0"."__ob_c1")), 7), 6) AS Nullable(Decimal(18, 6))) AS "Avg Return Fee"
  FROM (
    SELECT DISTINCT
      SUBSTRING(toString("Reason"."r_reason_desc"), 1, 20) AS "Reason Description 20",
      "Web Returns"."wr_item_sk" AS "__ob_k0",
      "Web Returns"."wr_order_number" AS "__ob_k1",
      "Web Returns"."wr_refunded_cash" AS "__ob_c0",
      "Web Returns"."wr_fee" AS "__ob_c1"
    FROM "tpcds"."web_sales" AS "Web Sales"
    LEFT JOIN "tpcds"."web_returns" AS "Web Returns"
      ON "Web Sales"."ws_item_sk" = "Web Returns"."wr_item_sk"
      AND "Web Sales"."ws_order_number" = "Web Returns"."wr_order_number"
    LEFT JOIN "tpcds"."reason" AS "Reason"
      ON "Web Returns"."wr_reason_sk" = "Reason"."r_reason_sk"
    LEFT JOIN "tpcds"."date_dim" AS "Web Sold Date"
      ON "Web Sales"."ws_sold_date_sk" = "Web Sold Date"."d_date_sk"
    LEFT JOIN "tpcds"."customer_demographics" AS "Refunded Demographics"
      ON "Web Returns"."wr_refunded_cdemo_sk" = "Refunded Demographics"."cd_demo_sk"
    LEFT JOIN "tpcds"."customer_demographics" AS "Returning Demographics"
      ON "Web Returns"."wr_returning_cdemo_sk" = "Returning Demographics"."cd_demo_sk"
    LEFT JOIN "tpcds"."customer_address" AS "Refunded Address"
      ON "Web Returns"."wr_refunded_addr_sk" = "Refunded Address"."ca_address_sk"
    WHERE
      "Web Sold Date"."d_year" = 2000
      AND (
        "Refunded Demographics"."cd_marital_status" = 'M'
        AND (
          "Refunded Demographics"."cd_marital_status" = "Returning Demographics"."cd_marital_status"
        ) = TRUE
        AND "Refunded Demographics"."cd_education_status" = 'Advanced Degree'
        AND (
          "Refunded Demographics"."cd_education_status" = "Returning Demographics"."cd_education_status"
        ) = TRUE
        AND "Web Sales"."ws_sales_price" BETWEEN 100.0 AND 150.0
        OR "Refunded Demographics"."cd_marital_status" = 'S'
        AND (
          "Refunded Demographics"."cd_marital_status" = "Returning Demographics"."cd_marital_status"
        ) = TRUE
        AND "Refunded Demographics"."cd_education_status" = 'College'
        AND (
          "Refunded Demographics"."cd_education_status" = "Returning Demographics"."cd_education_status"
        ) = TRUE
        AND "Web Sales"."ws_sales_price" BETWEEN 50.0 AND 100.0
        OR "Refunded Demographics"."cd_marital_status" = 'W'
        AND (
          "Refunded Demographics"."cd_marital_status" = "Returning Demographics"."cd_marital_status"
        ) = TRUE
        AND "Refunded Demographics"."cd_education_status" = '2 yr Degree'
        AND (
          "Refunded Demographics"."cd_education_status" = "Returning Demographics"."cd_education_status"
        ) = TRUE
        AND "Web Sales"."ws_sales_price" BETWEEN 150.0 AND 200.0
      )
      AND (
        "Refunded Address"."ca_country" = 'United States'
        AND "Refunded Address"."ca_state" IN ('IN', 'OH', 'NJ')
        AND "Web Sales"."ws_net_profit" BETWEEN 100 AND 200
        OR "Refunded Address"."ca_country" = 'United States'
        AND "Refunded Address"."ca_state" IN ('WI', 'CT', 'KY')
        AND "Web Sales"."ws_net_profit" BETWEEN 150 AND 300
        OR "Refunded Address"."ca_country" = 'United States'
        AND "Refunded Address"."ca_state" IN ('LA', 'IA', 'AR')
        AND "Web Sales"."ws_net_profit" BETWEEN 50 AND 250
      )
  ) AS "__ob_dedup_src_0"
  WHERE
    NOT (
      "__ob_dedup_src_0"."__ob_k0" IS NULL
    )
    AND NOT (
      "__ob_dedup_src_0"."__ob_k1" IS NULL
    )
  GROUP BY ALL
)
SELECT
  "__ob_main"."Reason Description 20" AS "Reason Description 20",
  "__ob_main"."Avg Web Quantity" AS "Avg Web Quantity",
  "__ob_dedup_0"."Avg Refunded Cash" AS "Avg Refunded Cash",
  "__ob_dedup_0"."Avg Return Fee" AS "Avg Return Fee"
FROM "__ob_main" AS "__ob_main"
LEFT JOIN "__ob_dedup_0" AS "__ob_dedup_0"
  ON "__ob_main"."Reason Description 20" = "__ob_dedup_0"."Reason Description 20"
  OR "__ob_main"."Reason Description 20" IS NULL
  AND "__ob_dedup_0"."Reason Description 20" IS NULL
ORDER BY
  "__ob_main"."Reason Description 20" ASC,
  "__ob_main"."Avg Web Quantity" ASC,
  "__ob_dedup_0"."Avg Refunded Cash" ASC,
  "__ob_dedup_0"."Avg Return Fee" ASC
LIMIT 100

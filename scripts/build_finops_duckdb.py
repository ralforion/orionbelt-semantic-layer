"""Build the FOCUS FinOps showcase DuckDB file.

Creates ``examples/finops.duckdb`` with four tables that mirror the datasets
defined by FOCUS (the FinOps Open Cost and Usage Specification, the FinOps
Foundation's vendor-neutral schema for technology cost and usage data):

    billing_periods   one row per billing account per month  (conformed root)
    charges           one row per resource per SKU per day   (fact)
    invoice_details   one row per invoice line               (fact)
    commitments       one row per contract commitment        (dimension)

``charges`` and ``invoice_details`` are deliberately *independent* facts that
only meet at ``billing_periods``. That is what makes invoice reconciliation
("does what we were charged match what we were invoiced?") a multi-fact
question, and it is the case the composite fact layer exists to answer.

Only standard FOCUS columns are generated - every column here is scalar, which
is true of the specification itself. Provider-specific extensions (the ``x_``
columns in a real Google Cloud export) are nested and are not modelled here.

The dataset is written as **raw JSONL first**, one JSON object per line under
``examples/finops_data/``, and only then loaded into DuckDB. That ordering is
the point: a FOCUS export is a file you receive, not a table someone hands you,
and the files stay on disk afterwards so they can be opened, grepped and diffed
before anything is modelled.

``charges.jsonl`` carries ``Tags`` as a **nested JSON object**, which is what a
real export looks like. The load flattens it back to JSON text so the model's
``json_value`` calls can read it; keeping it a DuckDB STRUCT would need the
nested-column support that does not exist yet.

Run:
    uv run python scripts/build_finops_duckdb.py

The generated .duckdb file is gitignored and rebuilt on demand, matching how
``build_demo_duckdb.py`` treats ``orionbelt_1_commerce.duckdb``.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb

# This script lives at <repo>/scripts/build_finops_duckdb.py, so the repo root is
# its parent's parent. Anchoring on __file__ keeps it correct regardless of the
# caller's working directory.
REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "examples" / "finops.duckdb"
# Raw export files, kept after the load so they can be inspected.
DATA_DIR = REPO / "examples" / "finops_data"
# A handful of pretty-printed charge records, committed so the shape of the
# export is visible in the repository without running the generator. The full
# JSONL is 20 MB and is gitignored.
SAMPLE = REPO / "examples" / "finops_charges_sample.json"
# Database is the domain (finops), schema is the specification the tables
# conform to (focus). Keeping them distinct also avoids DuckDB refusing a schema
# whose name collides with the catalog name taken from the file.
SCHEMA = "focus"

SEED = 20260816
random.seed(SEED)

CURRENCY = "EUR"

# ---------------------------------------------------------------------------
# Reference data
#
# ServiceCategory values are the FOCUS-defined set; ChargeCategory and
# PricingCategory likewise. Keeping to the specification's allowed values is
# the point of the showcase, so they are not invented here.
# ---------------------------------------------------------------------------

# (ProviderName, PublisherName, InvoiceIssuerName)
PROVIDERS = [
    ("Amazon Web Services", "Amazon Web Services", "Amazon Web Services"),
    ("Microsoft Azure", "Microsoft", "Microsoft"),
    ("Google Cloud", "Google", "Google"),
]

# (ServiceName, ServiceCategory, ResourceType, ConsumedUnit, list price per unit)
SERVICES = {
    "Amazon Web Services": [
        ("Amazon EC2", "Compute", "Instance", "Hours", 0.096),
        ("Amazon S3", "Storage", "Bucket", "GB-Months", 0.023),
        ("Amazon RDS", "Databases", "Database Instance", "Hours", 0.145),
        ("AWS Lambda", "Compute", "Function", "GB-Seconds", 0.0000167),
        ("Amazon CloudFront", "Networking", "Distribution", "GB", 0.085),
        ("Amazon EKS", "Compute", "Cluster", "Hours", 0.10),
    ],
    "Microsoft Azure": [
        ("Virtual Machines", "Compute", "Virtual Machine", "Hours", 0.104),
        ("Blob Storage", "Storage", "Storage Account", "GB-Months", 0.0184),
        ("Azure SQL Database", "Databases", "Database", "Hours", 0.152),
        ("Azure Functions", "Compute", "Function App", "GB-Seconds", 0.000016),
        ("Azure CDN", "Networking", "Endpoint", "GB", 0.081),
        ("Azure Kubernetes Service", "Compute", "Cluster", "Hours", 0.10),
    ],
    "Google Cloud": [
        ("Compute Engine", "Compute", "Instance", "Hours", 0.089),
        ("Cloud Storage", "Storage", "Bucket", "GB-Months", 0.020),
        ("Cloud SQL", "Databases", "Database Instance", "Hours", 0.138),
        ("Cloud Run", "Compute", "Service", "vCPU-Seconds", 0.000024),
        ("Cloud CDN", "Networking", "Backend", "GB", 0.080),
        ("Google Kubernetes Engine", "Compute", "Cluster", "Hours", 0.10),
    ],
}

# (RegionId, RegionName, AvailabilityZone)
REGIONS = [
    ("eu-west-1", "EU (Ireland)", "eu-west-1a"),
    ("eu-central-1", "EU (Frankfurt)", "eu-central-1b"),
    ("us-east-1", "US East (N. Virginia)", "us-east-1a"),
    ("us-west-2", "US West (Oregon)", "us-west-2c"),
    ("ap-southeast-1", "Asia Pacific (Singapore)", "ap-southeast-1a"),
]

# (SubAccountId, SubAccountName) - the cost-allocation unit (AWS account,
# Azure subscription, Google Cloud project).
SUB_ACCOUNTS = [
    ("sub-1001", "platform-prod"),
    ("sub-1002", "platform-staging"),
    ("sub-1003", "data-analytics"),
    ("sub-1004", "ml-research"),
    ("sub-1005", "corp-shared-services"),
]

# FOCUS defines Tags as a standard key-value column. Real exports carry it as
# JSON (Azure), a MAP (Databricks) or an ARRAY<STRUCT> (Google Cloud); JSON is
# the shape the portable json_value catalog entry reads, so that is what the
# generator writes. Tags are keyed off the sub-account so cost allocation by
# team actually reconciles with allocation by sub-account.
SUB_ACCOUNT_TAGS = {
    "sub-1001": {"team": "platform", "env": "prod", "cost_center": "cc-1200"},
    "sub-1002": {"team": "platform", "env": "staging", "cost_center": "cc-1200"},
    "sub-1003": {"team": "data", "env": "prod", "cost_center": "cc-1310"},
    "sub-1004": {"team": "ml", "env": "dev", "cost_center": "cc-1450"},
    "sub-1005": {"team": "shared", "env": "prod", "cost_center": "cc-1000"},
}

# A slice of rows carries no tags at all. Untagged spend is the number a FinOps
# practitioner actually chases, so a showcase that tags everything hides the
# one finding the query exists to surface.
UNTAGGED_RATE = 0.12

BILLING_ACCOUNT_ID = "acct-0001"
BILLING_ACCOUNT_NAME = "Contoso Group"

# Six months of billing periods.
PERIODS = [(2026, m) for m in range(3, 9)]


def _stable_id(*parts: str) -> int:
    """A five-digit id derived from *parts*, stable across processes.

    Not ``hash()``: Python salts string hashing per process unless
    PYTHONHASHSEED is pinned, so the same provider/service/region produced a
    different SKU on every run. That made the dataset irreproducible and, since
    the generator also rewrites the committed sample, dirtied the checkout
    every time the notebook was run.
    """
    digest = hashlib.sha256("\x00".join(parts).encode()).digest()
    return int.from_bytes(digest[:4], "big") % 90000 + 10000


def tags_for(sub_account_id: str) -> str | None:
    """The FOCUS Tags value for a sub-account, or None for an untagged row."""
    if random.random() < UNTAGGED_RATE:
        return None
    tags = SUB_ACCOUNT_TAGS.get(sub_account_id)
    return json.dumps(tags, separators=(",", ":")) if tags else None


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """Return the first day of the month and the first day of the next month."""
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


# ---------------------------------------------------------------------------
# Commitments
# ---------------------------------------------------------------------------


def build_commitments() -> list[tuple]:
    """One commitment per provider per category, spanning the whole window."""
    rows = []
    specs = [
        ("Savings Plan", "Usage", "Amazon Web Services", 45000.0, "Compute"),
        ("Reserved Instance", "Usage", "Amazon Web Services", 22000.0, "Databases"),
        ("Reservation", "Usage", "Microsoft Azure", 38000.0, "Compute"),
        ("Savings Plan", "Spend", "Microsoft Azure", 15000.0, "Databases"),
        ("Committed Use Discount", "Spend", "Google Cloud", 41000.0, "Compute"),
        ("Committed Use Discount", "Usage", "Google Cloud", 12000.0, "Databases"),
    ]
    start, _ = month_bounds(*PERIODS[0])
    _, end = month_bounds(*PERIODS[-1])
    for i, (ctype, category, provider, amount, service_category) in enumerate(specs, 1):
        rows.append(
            (
                f"cd-{i:03d}",
                f"{provider} {ctype} {i}",
                ctype,
                category,
                "Active",
                provider,
                service_category,
                datetime.combine(start, datetime.min.time()),
                datetime.combine(end, datetime.min.time()),
                round(amount, 2),
                CURRENCY,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------


def build_charges(commitments: list[tuple]) -> list[tuple]:
    """Daily usage charges, plus a monthly tax/purchase charge per provider.

    Discounting is modelled the way FOCUS intends it: ListCost is what the
    published rate would have cost, ContractedCost applies the negotiated rate,
    and EffectiveCost additionally amortises commitment coverage. Rows covered
    by a commitment carry its CommitmentDiscountId, which is what lets a query
    ask "how much of our committed spend did we actually use?".
    """
    # Commitments indexed by (provider, service category) so a charge can be
    # matched to the commitment that would plausibly cover it.
    by_provider: dict[tuple[str, str], str] = {(row[5], row[6]): row[0] for row in commitments}
    commitment_meta = {row[0]: (row[2], row[3], row[4]) for row in commitments}

    rows: list[tuple] = []
    for year, month in PERIODS:
        p_start, p_end = month_bounds(year, month)
        n_days = (p_end - p_start).days

        for provider, publisher, issuer in PROVIDERS:
            for service_name, service_cat, resource_type, unit, list_rate in SERVICES[provider]:
                # A stable handful of resources per service, so ResourceId is
                # meaningful across periods rather than random noise.
                n_resources = random.randint(3, 6)
                for r in range(n_resources):
                    sub_id, sub_name = random.choice(SUB_ACCOUNTS)
                    region_id, region_name, az = random.choice(REGIONS)
                    resource_id = (
                        f"{provider[:3].lower()}-{service_name[:4].lower().strip()}"
                        f"-{region_id}-{r:02d}"
                    ).replace(" ", "")
                    resource_name = f"{service_name} {r + 1}"
                    sku_id = f"SKU-{_stable_id(provider, service_name, region_id)}"
                    sku_price_id = f"{sku_id}-P{r % 3 + 1}"

                    commitment_id = by_provider.get((provider, service_cat))
                    # Only ~55% of eligible rows are actually covered.
                    covered = commitment_id is not None and random.random() < 0.55

                    # A gentle upward usage trend across the window, plus noise.
                    trend = 1.0 + 0.06 * PERIODS.index((year, month))
                    base = random.uniform(40, 900) * trend

                    for d in range(n_days):
                        day = p_start + timedelta(days=d)
                        # Weekends are quieter for non-storage services.
                        weekend = day.weekday() >= 5
                        factor = 0.55 if (weekend and service_cat != "Storage") else 1.0
                        qty = round(base * factor * random.uniform(0.85, 1.15), 4)
                        if qty <= 0:
                            continue

                        list_cost = qty * list_rate
                        # Negotiated rate: 0-12% off list.
                        contracted_rate = list_rate * (1 - random.uniform(0.0, 0.12))
                        contracted_cost = qty * contracted_rate
                        # BilledCost is the cash that lands on the invoice for
                        # this row; EffectiveCost additionally amortises the
                        # benefit of any commitment covering it. They differ
                        # only where a commitment applies, which is exactly the
                        # distinction FOCUS draws between the two columns.
                        billed_cost = contracted_cost
                        if covered:
                            effective_cost = contracted_cost * (1 - random.uniform(0.15, 0.35))
                        else:
                            effective_cost = contracted_cost

                        ctype, ccat, cstatus = (
                            commitment_meta[commitment_id] if covered else (None, None, None)
                        )

                        rows.append(
                            (
                                BILLING_ACCOUNT_ID,
                                BILLING_ACCOUNT_NAME,
                                sub_id,
                                sub_name,
                                datetime.combine(p_start, datetime.min.time()),
                                datetime.combine(p_end, datetime.min.time()),
                                datetime.combine(day, datetime.min.time()),
                                datetime.combine(day + timedelta(days=1), datetime.min.time()),
                                provider,
                                publisher,
                                issuer,
                                service_name,
                                service_cat,
                                sku_id,
                                sku_price_id,
                                resource_id,
                                resource_name,
                                resource_type,
                                region_id,
                                region_name,
                                az,
                                "Usage",
                                None,
                                f"{service_name} {resource_type.lower()} usage",
                                "Usage-Based",
                                "Standard" if not covered else "Committed",
                                round(qty, 4),
                                unit,
                                round(qty, 4),
                                unit,
                                round(list_rate, 8),
                                round(contracted_rate, 8),
                                round(list_cost, 6),
                                round(contracted_cost, 6),
                                round(billed_cost, 6),
                                round(effective_cost, 6),
                                CURRENCY,
                                commitment_id if covered else None,
                                ctype,
                                ccat,
                                cstatus,
                                tags_for(sub_id),
                            )
                        )

            # One tax charge per provider per period, so ChargeCategory has a
            # non-Usage value and cost-vs-invoice reconciliation has something
            # real to explain.
            tax = round(random.uniform(400, 1800), 2)
            rows.append(
                (
                    BILLING_ACCOUNT_ID,
                    BILLING_ACCOUNT_NAME,
                    SUB_ACCOUNTS[0][0],
                    SUB_ACCOUNTS[0][1],
                    datetime.combine(p_start, datetime.min.time()),
                    datetime.combine(p_end, datetime.min.time()),
                    datetime.combine(p_start, datetime.min.time()),
                    datetime.combine(p_end, datetime.min.time()),
                    provider,
                    publisher,
                    issuer,
                    "Tax",
                    "Other",
                    "SKU-TAX",
                    "SKU-TAX-P1",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "Tax",
                    None,
                    "Value added tax",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    tax,
                    tax,
                    tax,
                    tax,
                    CURRENCY,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
    return rows


def rightsize_commitments(commitments: list[tuple], charges: list[tuple]) -> list[tuple]:
    """Set each commitment's contract value from the spend it actually covered.

    Commitment amounts cannot be picked up front: they have to exceed the spend
    drawn against them, or utilisation comes out above 100% and the metric reads
    as nonsense. Working backwards from the generated charges gives every
    commitment a plausible utilisation in the 62-94% band, which is also the
    range where the "you are wasting committed spend" conversation happens.
    """
    i_effective, i_commitment = 35, 37

    drawn: dict[str, float] = {}
    for row in charges:
        cid = row[i_commitment]
        if cid:
            drawn[cid] = drawn.get(cid, 0.0) + float(row[i_effective] or 0.0)

    out = []
    for row in commitments:
        cid = row[0]
        covered = drawn.get(cid, 0.0)
        target_utilisation = random.uniform(0.62, 0.94)
        amount = round(covered / target_utilisation, 2) if covered else row[9]
        out.append(row[:9] + (amount,) + row[10:])
    return out


# ---------------------------------------------------------------------------
# Invoice details
# ---------------------------------------------------------------------------


def build_invoice_details(charges: list[tuple]) -> list[tuple]:
    """Invoice lines, summarised from charges but deliberately not identical.

    Real invoices differ from the usage export: they round, they land credits
    and adjustments at invoice level, and they arrive late. Reproducing that
    drift is the whole point - a reconciliation query that always returns zero
    variance would demonstrate nothing.
    """
    # Index of charge columns used below, kept local so the tuple layout above
    # stays the single source of truth. These must track the CREATE TABLE
    # column order for finops.charges.
    i_period_start, i_provider, i_billed = 4, 8, 34

    totals: dict[tuple, float] = {}
    for row in charges:
        key = (row[i_period_start], row[i_provider])
        totals[key] = totals.get(key, 0.0) + (row[i_billed] or 0.0)

    rows = []
    line_no = 0
    for (period_start, provider), billed in sorted(
        totals.items(), key=lambda kv: (kv[0][0], kv[0][1])
    ):
        issuer = next(p[2] for p in PROVIDERS if p[0] == provider)
        period_end = (
            date(period_start.year + 1, 1, 1)
            if period_start.month == 12
            else date(period_start.year, period_start.month + 1, 1)
        )
        invoice_id = f"INV-{provider[:3].upper()}-{period_start:%Y%m}"

        # Split the provider's monthly total across a few invoice lines, then
        # nudge it so reconciliation finds a small, explainable variance.
        drift = billed * random.uniform(-0.004, 0.004)
        invoiced_total = billed + drift
        n_lines = random.randint(2, 4)
        weights = [random.uniform(0.5, 1.5) for _ in range(n_lines)]
        wsum = sum(weights)
        for li, w in enumerate(weights, 1):
            line_no += 1
            amount = invoiced_total * (w / wsum)
            tax = amount * 0.19
            rows.append(
                (
                    invoice_id,
                    li,
                    issuer,
                    provider,
                    BILLING_ACCOUNT_ID,
                    datetime.combine(period_start.date(), datetime.min.time())
                    if isinstance(period_start, datetime)
                    else datetime.combine(period_start, datetime.min.time()),
                    datetime.combine(period_end, datetime.min.time()),
                    f"{provider} services line {li}",
                    round(amount, 2),
                    round(tax, 2),
                    round(amount + tax, 2),
                    CURRENCY,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Billing periods
# ---------------------------------------------------------------------------


def build_billing_periods() -> list[tuple]:
    rows = []
    for year, month in PERIODS:
        start, end = month_bounds(year, month)
        rows.append(
            (
                datetime.combine(start, datetime.min.time()),
                datetime.combine(end, datetime.min.time()),
                f"{year}-{month:02d}",
                year,
                month,
                start.strftime("%B %Y"),
                BILLING_ACCOUNT_ID,
                BILLING_ACCOUNT_NAME,
                CURRENCY,
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

# Column names in DDL order, so a row tuple can be zipped into a JSON object
# without a second source of truth. A mismatch here surfaces immediately as a
# DuckDB bind error on load rather than as silently shifted columns.
COLUMNS: dict[str, tuple[str, ...]] = {
    "providers": ("ProviderName", "PublisherName", "InvoiceIssuerName"),
    "billing_periods": (
        "BillingPeriodStart",
        "BillingPeriodEnd",
        "BillingPeriodKey",
        "BillingPeriodYear",
        "BillingPeriodMonth",
        "BillingPeriodLabel",
        "BillingAccountId",
        "BillingAccountName",
        "BillingCurrency",
    ),
    "commitments": (
        "CommitmentDiscountId",
        "CommitmentDiscountName",
        "CommitmentDiscountType",
        "CommitmentDiscountCategory",
        "CommitmentDiscountStatus",
        "ProviderName",
        "ServiceCategory",
        "CommitmentStart",
        "CommitmentEnd",
        "CommittedAmount",
        "BillingCurrency",
    ),
    "charges": (
        "BillingAccountId",
        "BillingAccountName",
        "SubAccountId",
        "SubAccountName",
        "BillingPeriodStart",
        "BillingPeriodEnd",
        "ChargePeriodStart",
        "ChargePeriodEnd",
        "ProviderName",
        "PublisherName",
        "InvoiceIssuerName",
        "ServiceName",
        "ServiceCategory",
        "SkuId",
        "SkuPriceId",
        "ResourceId",
        "ResourceName",
        "ResourceType",
        "RegionId",
        "RegionName",
        "AvailabilityZone",
        "ChargeCategory",
        "ChargeClass",
        "ChargeDescription",
        "ChargeFrequency",
        "PricingCategory",
        "PricingQuantity",
        "PricingUnit",
        "ConsumedQuantity",
        "ConsumedUnit",
        "ListUnitPrice",
        "ContractedUnitPrice",
        "ListCost",
        "ContractedCost",
        "BilledCost",
        "EffectiveCost",
        "BillingCurrency",
        "CommitmentDiscountId",
        "CommitmentDiscountType",
        "CommitmentDiscountCategory",
        "CommitmentDiscountStatus",
        "Tags",
    ),
    "invoice_details": (
        "InvoiceId",
        "InvoiceLineNumber",
        "InvoiceIssuerName",
        "ProviderName",
        "BillingAccountId",
        "BillingPeriodStart",
        "BillingPeriodEnd",
        "InvoiceLineDetail",
        "InvoicedAmount",
        "InvoiceTaxAmount",
        "InvoiceTotalAmount",
        "BillingCurrency",
    ),
}


def _jsonable(value: object) -> object:
    """Render a row value in the form a real JSON export would carry."""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, Decimal):
        return float(value)
    return value


def write_jsonl(table: str, rows: list[tuple]) -> Path:
    """Write *rows* as one JSON object per line and return the path.

    ``Tags`` is emitted as a nested object rather than an escaped string: the
    file is meant to be read by a person, and a real export nests it.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{table}.jsonl"
    names = COLUMNS[table]
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            record = {n: _jsonable(v) for n, v in zip(names, row, strict=True)}
            if record.get("Tags"):
                record["Tags"] = json.loads(record["Tags"])
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

DROP TABLE IF EXISTS {SCHEMA}.charges;
DROP TABLE IF EXISTS {SCHEMA}.invoice_details;
DROP TABLE IF EXISTS {SCHEMA}.commitments;
DROP TABLE IF EXISTS {SCHEMA}.billing_periods;
DROP TABLE IF EXISTS {SCHEMA}.providers;

CREATE TABLE {SCHEMA}.providers (
    ProviderName        VARCHAR,
    PublisherName       VARCHAR,
    InvoiceIssuerName   VARCHAR
);

CREATE TABLE {SCHEMA}.billing_periods (
    BillingPeriodStart   TIMESTAMP,
    BillingPeriodEnd     TIMESTAMP,
    BillingPeriodKey     VARCHAR,
    BillingPeriodYear    INTEGER,
    BillingPeriodMonth   INTEGER,
    BillingPeriodLabel   VARCHAR,
    BillingAccountId     VARCHAR,
    BillingAccountName   VARCHAR,
    BillingCurrency      VARCHAR
);

CREATE TABLE {SCHEMA}.commitments (
    CommitmentDiscountId        VARCHAR,
    CommitmentDiscountName      VARCHAR,
    CommitmentDiscountType      VARCHAR,
    CommitmentDiscountCategory  VARCHAR,
    CommitmentDiscountStatus    VARCHAR,
    ProviderName                VARCHAR,
    ServiceCategory             VARCHAR,
    CommitmentStart             TIMESTAMP,
    CommitmentEnd               TIMESTAMP,
    CommittedAmount             DECIMAL(18, 2),
    BillingCurrency             VARCHAR
);

CREATE TABLE {SCHEMA}.charges (
    BillingAccountId            VARCHAR,
    BillingAccountName          VARCHAR,
    SubAccountId                VARCHAR,
    SubAccountName              VARCHAR,
    BillingPeriodStart          TIMESTAMP,
    BillingPeriodEnd            TIMESTAMP,
    ChargePeriodStart           TIMESTAMP,
    ChargePeriodEnd             TIMESTAMP,
    ProviderName                VARCHAR,
    PublisherName               VARCHAR,
    InvoiceIssuerName           VARCHAR,
    ServiceName                 VARCHAR,
    ServiceCategory             VARCHAR,
    SkuId                       VARCHAR,
    SkuPriceId                  VARCHAR,
    ResourceId                  VARCHAR,
    ResourceName                VARCHAR,
    ResourceType                VARCHAR,
    RegionId                    VARCHAR,
    RegionName                  VARCHAR,
    AvailabilityZone            VARCHAR,
    ChargeCategory              VARCHAR,
    ChargeClass                 VARCHAR,
    ChargeDescription           VARCHAR,
    ChargeFrequency             VARCHAR,
    PricingCategory             VARCHAR,
    PricingQuantity             DECIMAL(18, 4),
    PricingUnit                 VARCHAR,
    ConsumedQuantity            DECIMAL(18, 4),
    ConsumedUnit                VARCHAR,
    ListUnitPrice               DECIMAL(18, 8),
    ContractedUnitPrice         DECIMAL(18, 8),
    ListCost                    DECIMAL(18, 6),
    ContractedCost              DECIMAL(18, 6),
    BilledCost                  DECIMAL(18, 6),
    EffectiveCost               DECIMAL(18, 6),
    BillingCurrency             VARCHAR,
    CommitmentDiscountId        VARCHAR,
    CommitmentDiscountType      VARCHAR,
    CommitmentDiscountCategory  VARCHAR,
    CommitmentDiscountStatus    VARCHAR,
    Tags                        VARCHAR
);

CREATE TABLE {SCHEMA}.invoice_details (
    InvoiceId           VARCHAR,
    InvoiceLineNumber   INTEGER,
    InvoiceIssuerName   VARCHAR,
    ProviderName        VARCHAR,
    BillingAccountId    VARCHAR,
    BillingPeriodStart  TIMESTAMP,
    BillingPeriodEnd    TIMESTAMP,
    InvoiceLineDetail   VARCHAR,
    InvoicedAmount      DECIMAL(18, 2),
    InvoiceTaxAmount    DECIMAL(18, 2),
    InvoiceTotalAmount  DECIMAL(18, 2),
    BillingCurrency     VARCHAR
);
"""


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    con = duckdb.connect(str(OUT))
    con.execute(DDL)

    periods = build_billing_periods()
    commitments = build_commitments()
    charges = build_charges(commitments)
    commitments = rightsize_commitments(commitments, charges)
    invoices = build_invoice_details(charges)

    # Provider is the one dimension both facts share besides the billing
    # period. Giving it its own table makes it a conformed dimension, so a
    # cross-fact query can group by it; left inline on each fact it would only
    # populate on whichever leg happened to own the column.
    tables = {
        "providers": [tuple(r) for r in PROVIDERS],
        "billing_periods": periods,
        "commitments": commitments,
        "charges": charges,
        "invoice_details": invoices,
    }

    # Stage 1: write the raw export. These files are the artefact a user reads.
    for table, rows in tables.items():
        path = write_jsonl(table, rows)
        size = path.stat().st_size
        print(f"  wrote {path.relative_to(REPO)}  ({len(rows):,} rows, {size / 1024:,.0f} KB)")

    # A committed excerpt, so a reader who has not run the generator can still
    # see what the raw export looks like.
    with (DATA_DIR / "charges.jsonl").open(encoding="utf-8") as fh:
        excerpt = [json.loads(next(fh)) for _ in range(3)]
    SAMPLE.write_text(json.dumps(excerpt, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {SAMPLE.relative_to(REPO)}  (3-record excerpt, committed)")

    # Stage 2: load the export into DuckDB. INSERT ... BY NAME matches on
    # column name rather than position, so the JSON key order cannot silently
    # shift a column, and DuckDB casts each value into the declared type.
    #
    # Tags is the exception: it is a nested object in the file and has to land
    # as JSON *text* for the model's json_value calls to read it. A DuckDB
    # STRUCT would need nested-column support that does not exist yet.
    print()
    for table in tables:
        src = DATA_DIR / f"{table}.jsonl"
        if table == "charges":
            projected = ", ".join(
                "to_json(Tags) AS Tags" if c == "Tags" else c for c in COLUMNS[table]
            )
            con.execute(
                f"INSERT INTO {SCHEMA}.{table} BY NAME "
                f"SELECT {projected} FROM read_json_auto('{src.as_posix()}')"
            )
        else:
            con.execute(
                f"INSERT INTO {SCHEMA}.{table} BY NAME "
                f"SELECT * FROM read_json_auto('{src.as_posix()}')"
            )

    print(f"providers       : {len(PROVIDERS):>8,}")
    print(f"billing_periods : {len(periods):>8,}")
    print(f"commitments     : {len(commitments):>8,}")
    print(f"charges         : {len(charges):>8,}")
    print(f"invoice_details : {len(invoices):>8,}")

    billed, effective, listc = con.execute(
        f"SELECT SUM(BilledCost), SUM(EffectiveCost), SUM(ListCost) FROM {SCHEMA}.charges"
    ).fetchone()
    invoiced = con.execute(f"SELECT SUM(InvoicedAmount) FROM {SCHEMA}.invoice_details").fetchone()[
        0
    ]
    print(
        f"\nbilled={billed:,.2f} effective={effective:,.2f} list={listc:,.2f} "
        f"invoiced={invoiced:,.2f} variance={billed - invoiced:,.2f} {CURRENCY}"
    )

    con.close()
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()

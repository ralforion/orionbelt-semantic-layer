"""Build the demo DuckDB file for the public OrionBelt deploy.

Creates ``examples/orionbelt_1_commerce.duckdb`` with the 15 tables that match
``examples/orionbelt_1_commerce.yaml`` and seeds ~medium-size sample data so the
deployed API can answer real /v1/query/execute calls without external infra.

Run:
    uv run python scripts/build_demo_duckdb.py

The generated .duckdb file is gitignored; it is rebuilt on demand by the deploy
pipeline (deploy-gcloud.sh), the Docker Hub publish workflow, and the Dremio
demo before they need it.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

# This script lives at <repo>/scripts/build_demo_duckdb.py, so the repo root is
# its parent's parent. Anchoring on __file__ keeps it correct regardless of the
# caller's working directory.
REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "examples" / "orionbelt_1_commerce.duckdb"
SCHEMA = "orionbelt_1"

SEED = 20260508
random.seed(SEED)


# ---------------------------------------------------------------------------
# Static reference data
# ---------------------------------------------------------------------------

REGIONS = [
    ("R1", "North America"),
    ("R2", "Europe"),
    ("R3", "Asia Pacific"),
    ("R4", "Latin America"),
    ("R5", "Middle East & Africa"),
]

COUNTRIES = [
    ("US", "United States", "R1"),
    ("CA", "Canada", "R1"),
    ("MX", "Mexico", "R4"),
    ("BR", "Brazil", "R4"),
    ("AR", "Argentina", "R4"),
    ("DE", "Germany", "R2"),
    ("FR", "France", "R2"),
    ("UK", "United Kingdom", "R2"),
    ("IT", "Italy", "R2"),
    ("ES", "Spain", "R2"),
    ("NL", "Netherlands", "R2"),
    ("CH", "Switzerland", "R2"),
    ("HR", "Croatia", "R2"),
    ("AT", "Austria", "R2"),
    ("SE", "Sweden", "R2"),
    ("PL", "Poland", "R2"),
    ("JP", "Japan", "R3"),
    ("CN", "China", "R3"),
    ("IN", "India", "R3"),
    ("AU", "Australia", "R3"),
    ("SG", "Singapore", "R3"),
    ("KR", "South Korea", "R3"),
    ("AE", "United Arab Emirates", "R5"),
    ("ZA", "South Africa", "R5"),
    ("EG", "Egypt", "R5"),
]

BANKS = [
    ("B1", "First Continental"),
    ("B2", "Global Trust Bank"),
    ("B3", "Pacific Mercantile"),
    ("B4", "Atlantic Reserve"),
    ("B5", "Northern Capital"),
]

CHANNELS = [
    ("C1", "Online"),
    ("C2", "Retail"),
    ("C3", "Wholesale"),
    ("C4", "B2B"),
]

PRODUCT_CATEGORIES = [
    "Electronics",
    "Apparel",
    "Home & Kitchen",
    "Books",
    "Sports",
    "Beauty",
    "Toys",
    "Office",
    "Garden",
    "Automotive",
]

DEPARTMENTS = [
    "Sales",
    "Purchasing",
    "Logistics",
    "Customer Service",
    "Finance",
]

CURRENCIES = ["USD", "EUR", "GBP", "JPY"]

PAYMENT_TYPES = ["credit_card", "bank_transfer", "paypal", "invoice", "cash"]

SHIPMENT_TYPES = ["standard", "express", "overnight", "freight"]

FIRST_NAMES = [
    "Alex",
    "Bailey",
    "Casey",
    "Dana",
    "Erin",
    "Fran",
    "Gale",
    "Hayden",
    "Indigo",
    "Jordan",
    "Kai",
    "Logan",
    "Morgan",
    "Noel",
    "Ollie",
    "Parker",
    "Quinn",
    "Riley",
    "Sage",
    "Taylor",
    "Uriah",
    "Val",
    "Wren",
    "Xen",
    "Yael",
    "Zion",
    "Avery",
    "Blake",
    "Cameron",
    "Drew",
    "Eden",
    "Finley",
]

LAST_NAMES = [
    "Adler",
    "Bauer",
    "Costa",
    "Dvorak",
    "Eriksen",
    "Fischer",
    "Garcia",
    "Hassan",
    "Ivanov",
    "Jansen",
    "Klein",
    "Lopez",
    "Müller",
    "Nakamura",
    "O'Brien",
    "Petrov",
    "Quinn",
    "Rossi",
    "Schmidt",
    "Tanaka",
    "Ueda",
    "Vargas",
    "Weber",
    "Xu",
    "Yamamoto",
    "Zhang",
]

PRODUCT_PREFIXES = [
    "Pro",
    "Ultra",
    "Smart",
    "Eco",
    "Max",
    "Lite",
    "Plus",
    "Basic",
    "Studio",
    "Edge",
    "Prime",
    "Core",
]

PRODUCT_SUFFIXES = [
    "X1",
    "X2",
    "X3",
    "S",
    "M",
    "L",
    "XL",
    "2000",
    "Mini",
    "Air",
    "Go",
    "Touch",
    "One",
    "Duo",
    "Trio",
]

COMPLAINT_TEMPLATES = [
    "Late delivery on {ref}",
    "Damaged packaging — refund requested",
    "Wrong item shipped — needs replacement",
    "Quality below expectation",
    "Billing discrepancy on {ref}",
    "Slow customer support response",
    "Product missing accessories",
    "Defective unit — requesting RMA",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def daterange(start: date, end: date) -> list[date]:
    n = (end - start).days
    return [start + timedelta(days=i) for i in range(n + 1)]


def weekday_name(d: date) -> str:
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d.weekday()]


# Approximate set of US-style holidays (used as a marker on a fraction of days).
def is_holiday(d: date) -> bool:
    if (d.month, d.day) in {(1, 1), (7, 4), (12, 25), (12, 26), (5, 1), (11, 11)}:
        return True
    # Last Thursday of November (Thanksgiving-ish)
    return d.month == 11 and d.weekday() == 3 and d.day >= 22


def make_email(first: str, last: str, idx: int) -> str:
    domain = random.choice(["example.com", "demo.io", "sample.org", "mail.test"])
    return f"{first.lower()}.{last.lower()}{idx}@{domain}"


def make_iban(country: str, n: int) -> str:
    return f"{country}{(n % 100):02d}DEMO{n:016d}"


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


#: Sales and purchases are sampled from these dates.
SALES_START = date(2021, 1, 1)
SALES_END = date(2025, 12, 31)
#: The calendar runs past the sales window, so the dates that follow a sale
#: have a calendar row too: shipments go out up to 5 days after a sale and
#: returns come back up to 30 days after it. Without them, late-December
#: shipments and returns had no month and formed a NULL group.
CALENDAR_END = date(2026, 1, 31)


def gen_calendar() -> list[tuple[date, str, str, str]]:
    rows = []
    for d in daterange(SALES_START, CALENDAR_END):
        rows.append(
            (
                d,
                d.strftime("%Y-%m"),
                weekday_name(d),
                "Y" if is_holiday(d) else "N",
            )
        )
    return rows


def gen_suppliers(n: int) -> list[tuple[str, str, str]]:
    out = []
    for i in range(1, n + 1):
        brand = random.choice(
            [
                "Acme",
                "Globex",
                "Initech",
                "Soylent",
                "Umbrella",
                "Hooli",
                "Pied Piper",
                "Wayne",
                "Stark",
                "Wonka",
            ]
        )
        suffix = random.choice(["Industries", "Corp", "Holdings", "Partners", "Group", "Trading"])
        name = f"{brand} {suffix}"
        country = random.choice(COUNTRIES)[0]
        out.append((f"S{i:04d}", name, country))
    return out


def gen_employees(n: int) -> list[tuple[str, str, str]]:
    out = []
    seen = set()
    while len(out) < n:
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        full = f"{first} {last}"
        if full in seen:
            continue
        seen.add(full)
        out.append((f"E{len(out) + 1:04d}", full, random.choice(DEPARTMENTS)))
    return out


def gen_products(n: int, suppliers: list[tuple[str, str, str]]) -> list[tuple]:
    out = []
    for i in range(1, n + 1):
        name = (
            f"{random.choice(PRODUCT_PREFIXES)} "
            f"{random.choice(PRODUCT_CATEGORIES)} "
            f"{random.choice(PRODUCT_SUFFIXES)}"
        )
        cost = round(random.uniform(2.0, 800.0), 2)
        margin = random.uniform(1.15, 2.5)
        price = round(cost * margin, 2)
        out.append(
            (
                f"P{i:04d}",
                name,
                random.choice(PRODUCT_CATEGORIES),
                random.choice(suppliers)[0],
                cost,
                price,
                random.choice(CURRENCIES),
                random.randint(0, 1500),
            )
        )
    return out


def gen_clients(n: int) -> list[tuple]:
    out = []
    seen = set()
    while len(out) < n:
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        full = f"{first} {last}"
        if full in seen:
            continue
        seen.add(full)
        idx = len(out) + 1
        gender = random.choice(["F", "M", "X"])
        email = make_email(first, last, idx)
        country = random.choice(COUNTRIES)[0]
        out.append((f"CL{idx:05d}", full, gender, email, country))
    return out


def gen_account_balances(clients: list[tuple]) -> list[tuple]:
    out = []
    for i, c in enumerate(clients, start=1):
        country = c[4]
        bank = random.choice(BANKS)[0]
        # Balances skewed towards small/medium with occasional whales.
        amt = round(random.lognormvariate(7.5, 1.1), 2)
        out.append((f"A{i:05d}", make_iban(country, i), bank, amt))
    return out


# Year-over-year multiplier (~85% YoY growth across the 5-year window).
YEAR_MULT = {2021: 0.70, 2022: 0.85, 2023: 1.00, 2024: 1.20, 2025: 1.40}

# Monthly seasonality (Q4 spike, post-holiday dip).
MONTH_MULT = {
    1: 0.80,
    2: 0.80,
    3: 0.95,
    4: 1.00,
    5: 1.00,
    6: 0.95,
    7: 0.90,
    8: 0.90,
    9: 1.05,
    10: 1.10,
    11: 1.40,
    12: 1.65,
}


def date_weight(d: date) -> float:
    """Weight a calendar date for sampling. Captures growth + seasonality."""
    w = YEAR_MULT.get(d.year, 1.0) * MONTH_MULT[d.month]
    # Weekends are quieter for B2B-heavy ecommerce mixes.
    if d.weekday() >= 5:
        w *= 0.65
    if is_holiday(d):
        w *= 1.25
    return w


def pareto_weights(n: int, alpha: float = 1.16) -> list[float]:
    """Zipf-like weights so a small head dominates volume (~80/20 with alpha=1.16)."""
    return [1.0 / ((i + 1) ** alpha) for i in range(n)]


def gen_sales(
    n: int,
    clients: list[tuple],
    products: list[tuple],
    employees: list[tuple],
    calendar_dates: list[date],
) -> list[tuple]:
    """Generate n sales with realistic distributions:

    * Time: weighted by year (growth) × month (Q4 spike) × weekend dip × holiday boost
    * Clients: Pareto — top ~20% generate ~80% of orders
    * Products: Pareto — top ~20% generate ~80% of orders
    * Channels: Online > Retail > Wholesale > B2B
    * Country: US/DE/UK dominant via client country distribution
    * Employees: only Sales / Customer Service staff write orders
    * Quantity: usually small (1-5), occasional bulk orders
    """
    out = []
    sales_emps = [e for e in employees if e[2] in {"Sales", "Customer Service"}] or employees

    # Pre-shuffle clients/products so Pareto weights aren't correlated with ID order
    # — that gives the impression that any client/product can be a top performer.
    shuffled_clients = clients[:]
    random.shuffle(shuffled_clients)
    client_w = pareto_weights(len(shuffled_clients))

    shuffled_products = products[:]
    random.shuffle(shuffled_products)
    product_w = pareto_weights(len(shuffled_products))

    date_w = [date_weight(d) for d in calendar_dates]

    channels = [c[0] for c in CHANNELS]
    channel_w = [0.50, 0.30, 0.15, 0.05]  # Online, Retail, Wholesale, B2B

    payment_w = [0.40, 0.20, 0.20, 0.15, 0.05]  # credit, transfer, paypal, invoice, cash

    sample_dates = random.choices(calendar_dates, weights=date_w, k=n)
    sample_clients = random.choices(shuffled_clients, weights=client_w, k=n)
    sample_products = random.choices(shuffled_products, weights=product_w, k=n)
    sample_channels = random.choices(channels, weights=channel_w, k=n)
    sample_payments = random.choices(PAYMENT_TYPES, weights=payment_w, k=n)

    for i in range(1, n + 1):
        d = sample_dates[i - 1]
        c = sample_clients[i - 1][0]
        p = sample_products[i - 1]
        e = random.choice(sales_emps)[0]
        ch = sample_channels[i - 1]

        # 88% small orders (1-5 units), 10% medium (6-25), 2% bulk (26-200)
        roll = random.random()
        if roll < 0.88:
            qty = random.randint(1, 5)
        elif roll < 0.98:
            qty = random.randint(6, 25)
        else:
            qty = random.randint(26, 200)

        unit_price = float(p[5])
        # Discount modestly skewed by channel: wholesale/B2B get bigger discounts.
        if ch == "C3":  # Wholesale
            disc = random.uniform(0.70, 0.90)
        elif ch == "C4":  # B2B
            disc = random.uniform(0.75, 0.95)
        else:
            disc = random.uniform(0.92, 1.05)
        amt = round(qty * unit_price * disc, 2)
        out.append(
            (
                f"SO{i:07d}",
                d,
                c,
                p[0],
                e,
                sample_payments[i - 1],
                ch,
                float(qty),
                amt,
            )
        )
    return out


def gen_purchases(
    n: int,
    products: list[tuple],
    employees: list[tuple],
    suppliers: list[tuple],
    calendar_dates: list[date],
) -> list[tuple]:
    out = []
    purch_emps = [e for e in employees if e[2] == "Purchasing"] or employees
    # Lighter weighting than sales: purchasing schedule tracks sales-volume
    # growth but lacks the strong Q4 retail spike — buyers procure ahead of
    # the season, so dampen the monthly multipliers.
    date_w = [
        YEAR_MULT.get(d.year, 1.0) * (1.0 + 0.4 * (MONTH_MULT[d.month] - 1.0))
        for d in calendar_dates
    ]
    sample_dates = random.choices(calendar_dates, weights=date_w, k=n)
    for i in range(1, n + 1):
        d = sample_dates[i - 1]
        p = random.choice(products)
        e = random.choice(purch_emps)[0]
        s = random.choice(suppliers)[0]
        ch = random.choice(CHANNELS)[0]
        qty = random.randint(10, 500)
        unit_cost = float(p[4])
        price = round(unit_cost * random.uniform(0.95, 1.05), 2)
        amt = round(qty * price, 2)
        out.append(
            (
                f"PO{i:07d}",
                d,
                p[0],
                e,
                s,
                ch,
                float(qty),
                price,
                amt,
            )
        )
    return out


def gen_returns(
    sales: list[tuple],
    employees: list[tuple],
    pct: float = 0.05,
) -> list[tuple]:
    out = []
    cs_emps = [e for e in employees if e[2] in {"Customer Service", "Sales"}] or employees
    sample = random.sample(sales, k=int(len(sales) * pct))
    for i, s in enumerate(sample, start=1):
        sales_id = s[0]
        sales_date = s[1]
        ret_offset = random.randint(1, 30)
        ret_date = sales_date + timedelta(days=ret_offset)
        # Don't return more than was sold.
        ret_qty = random.uniform(0.1, 1.0) * float(s[7])
        ret_amt = round((ret_qty / float(s[7])) * float(s[8]), 2)
        out.append(
            (
                f"RE{i:07d}",
                sales_id,
                ret_date,
                random.choice(cs_emps)[0],
                round(ret_qty, 2),
                ret_amt,
            )
        )
    return out


def gen_shipments(
    sales: list[tuple],
    employees: list[tuple],
    pct: float = 0.95,
) -> list[tuple]:
    out = []
    log_emps = [e for e in employees if e[2] == "Logistics"] or employees
    shipped = random.sample(sales, k=int(len(sales) * pct))
    for i, s in enumerate(shipped, start=1):
        sales_id = s[0]
        sales_date = s[1]
        ship_date = sales_date + timedelta(days=random.randint(0, 5))
        out.append(
            (
                f"SH{i:07d}",
                sales_id,
                ship_date,
                random.choice(log_emps)[0],
                random.choice(SHIPMENT_TYPES),
                float(s[7]),
                float(s[8]),
            )
        )
    return out


def gen_complaints(
    n: int,
    clients: list[tuple],
    sales: list[tuple],
) -> list[tuple]:
    out = []
    for i in range(1, n + 1):
        c = random.choice(clients)[0]
        ref = random.choice(sales)[0]
        text = random.choice(COMPLAINT_TEMPLATES).format(ref=ref)
        out.append((f"CO{i:05d}", c, text))
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    print(f"Building {OUT.relative_to(REPO)} ...")

    calendar_rows = gen_calendar()
    suppliers = gen_suppliers(30)
    employees = gen_employees(60)
    products = gen_products(120, suppliers)
    clients = gen_clients(500)
    balances = gen_account_balances(clients)
    # Sample from the sales window only. The calendar's extra days must not
    # become sale dates, which would also change every value drawn after them.
    cal_dates = [r[0] for r in calendar_rows if r[0] <= SALES_END]
    sales = gen_sales(10_000, clients, products, employees, cal_dates)
    purchases = gen_purchases(3_000, products, employees, suppliers, cal_dates)
    returns = gen_returns(sales, employees, pct=0.05)
    shipments = gen_shipments(sales, employees, pct=0.95)
    complaints = gen_complaints(250, clients, sales)

    conn = duckdb.connect(database=str(OUT), read_only=False)
    try:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

        # Schema definitions follow examples/orionbelt_1_commerce.yaml exactly:
        # column codes (lowercase) match the OBML column.code values so the
        # compiler's generated SQL resolves without quoting.
        conn.execute(
            f"""
            CREATE TABLE {SCHEMA}.regions (
                regionid VARCHAR PRIMARY KEY,
                regionname VARCHAR
            );
            CREATE TABLE {SCHEMA}.countries (
                countryid VARCHAR PRIMARY KEY,
                countryname VARCHAR,
                region VARCHAR
            );
            CREATE TABLE {SCHEMA}.banks (
                bankid VARCHAR PRIMARY KEY,
                bankname VARCHAR
            );
            CREATE TABLE {SCHEMA}.suppliers (
                supplierid VARCHAR PRIMARY KEY,
                suppliername VARCHAR,
                suppliercountryid VARCHAR
            );
            CREATE TABLE {SCHEMA}.channels (
                channelid VARCHAR PRIMARY KEY,
                channelname VARCHAR
            );
            CREATE TABLE {SCHEMA}.employees (
                employeeid VARCHAR PRIMARY KEY,
                employeename VARCHAR,
                department VARCHAR
            );
            CREATE TABLE {SCHEMA}.products (
                productid VARCHAR PRIMARY KEY,
                productname VARCHAR,
                productcat VARCHAR,
                productsuppl VARCHAR,
                unitcost DOUBLE,
                unitprice DOUBLE,
                curr VARCHAR,
                unitsinstock DOUBLE
            );
            CREATE TABLE {SCHEMA}.clients (
                clientid VARCHAR PRIMARY KEY,
                clientname VARCHAR,
                clientgender VARCHAR,
                clientemail VARCHAR,
                clientcountryid VARCHAR
            );
            CREATE TABLE {SCHEMA}.acctbal (
                accountid VARCHAR PRIMARY KEY,
                iban VARCHAR,
                bankid VARCHAR,
                balanceamt DOUBLE
            );
            CREATE TABLE {SCHEMA}.calendar (
                date DATE PRIMARY KEY,
                ym VARCHAR,
                weekday VARCHAR,
                publicholiday VARCHAR
            );
            CREATE TABLE {SCHEMA}.sales (
                salesid VARCHAR PRIMARY KEY,
                salesdate DATE,
                salesclient VARCHAR,
                product VARCHAR,
                salesempid VARCHAR,
                salespaymenttype VARCHAR,
                saleschanid VARCHAR,
                salesquantity DOUBLE,
                salesamount DOUBLE
            );
            CREATE TABLE {SCHEMA}.purchases (
                purchaseid VARCHAR PRIMARY KEY,
                purchasedate DATE,
                purchaseproduct VARCHAR,
                purchaseempid VARCHAR,
                purchasesupplier VARCHAR,
                purchasechanid VARCHAR,
                purchasequantity DOUBLE,
                purchaseprice DOUBLE,
                purchaseamount DOUBLE
            );
            CREATE TABLE {SCHEMA}.returns (
                returnid VARCHAR PRIMARY KEY,
                returnsalesid VARCHAR,
                returndate DATE,
                returnempid VARCHAR,
                returnquantity DOUBLE,
                returnamount DOUBLE
            );
            CREATE TABLE {SCHEMA}.shipments (
                shipmentid VARCHAR PRIMARY KEY,
                shipmentsalesid VARCHAR,
                shipmentdate DATE,
                shipmentempid VARCHAR,
                shipmenttype VARCHAR,
                shipmentquantity DOUBLE,
                shipmentamount DOUBLE
            );
            CREATE TABLE {SCHEMA}.clientcomplaints (
                complid VARCHAR PRIMARY KEY,
                complclientid VARCHAR,
                compltext VARCHAR
            );
            """
        )

        # Bulk insert via executemany.
        conn.executemany(f"INSERT INTO {SCHEMA}.regions VALUES (?, ?)", REGIONS)
        conn.executemany(f"INSERT INTO {SCHEMA}.countries VALUES (?, ?, ?)", COUNTRIES)
        conn.executemany(f"INSERT INTO {SCHEMA}.banks VALUES (?, ?)", BANKS)
        conn.executemany(f"INSERT INTO {SCHEMA}.channels VALUES (?, ?)", CHANNELS)
        conn.executemany(f"INSERT INTO {SCHEMA}.suppliers VALUES (?, ?, ?)", suppliers)
        conn.executemany(f"INSERT INTO {SCHEMA}.employees VALUES (?, ?, ?)", employees)
        conn.executemany(f"INSERT INTO {SCHEMA}.products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", products)
        conn.executemany(f"INSERT INTO {SCHEMA}.clients VALUES (?, ?, ?, ?, ?)", clients)
        conn.executemany(f"INSERT INTO {SCHEMA}.acctbal VALUES (?, ?, ?, ?)", balances)
        conn.executemany(f"INSERT INTO {SCHEMA}.calendar VALUES (?, ?, ?, ?)", calendar_rows)
        conn.executemany(f"INSERT INTO {SCHEMA}.sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", sales)
        conn.executemany(
            f"INSERT INTO {SCHEMA}.purchases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            purchases,
        )
        conn.executemany(f"INSERT INTO {SCHEMA}.returns VALUES (?, ?, ?, ?, ?, ?)", returns)
        conn.executemany(f"INSERT INTO {SCHEMA}.shipments VALUES (?, ?, ?, ?, ?, ?, ?)", shipments)
        conn.executemany(f"INSERT INTO {SCHEMA}.clientcomplaints VALUES (?, ?, ?)", complaints)

        conn.commit()

        # Print summary so CI / human can sanity-check row counts.
        tables = [
            "regions",
            "countries",
            "banks",
            "channels",
            "suppliers",
            "employees",
            "products",
            "clients",
            "acctbal",
            "calendar",
            "sales",
            "purchases",
            "returns",
            "shipments",
            "clientcomplaints",
        ]
        print()
        print(f"{'table':<20} rows")
        print("-" * 30)
        for t in tables:
            count = conn.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{t}").fetchone()[0]
            print(f"{t:<20} {count:>9,}")
    finally:
        conn.close()

    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"\nWrote {OUT.relative_to(REPO)} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()

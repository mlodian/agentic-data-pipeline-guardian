"""Synthetic upstream-system data generator.

Simulates the kind of agentic data ingestion the Guardian project is built
to monitor: pulling from CRM, ERP, and HRIS-style upstream systems into raw
warehouse tables. In production this module would be replaced by real API
connectors (Salesforce, NetSuite, Workday, etc.) -- the point of this repo
is what happens *after* ingestion, so these connectors are intentionally
simple synthetic generators rather than real integrations.

Supports injecting a schema change or data-quality problems on demand, so
the Guardian agent has something real to catch. Run it twice:

    python -m ingestion.generate --seed 1                 # clean baseline
    python -m ingestion.generate --seed 2 --inject-schema-drift --inject-dq-issues
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone

import pandas as pd
from faker import Faker

from ingestion.db import connect

REGIONS = ["APAC", "EMEA", "NA", "LATAM"]
INDUSTRIES = ["Fintech", "Healthcare", "Retail", "Manufacturing", "SaaS", "Logistics"]
DEAL_STAGES = ["prospecting", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"]
DEPARTMENTS = ["Engineering", "Sales", "Compliance", "Finance", "Operations", "HR"]
INVOICE_STATUSES = ["paid", "unpaid", "overdue", "void"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def generate_crm_customers(fake: Faker, n: int) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        rows.append(
            {
                "customer_id": i,
                "company_name": fake.company(),
                "industry": random.choice(INDUSTRIES),
                "region": random.choice(REGIONS),
                "signup_date": fake.date_between(start_date="-3y", end_date="-30d"),
                "account_owner": fake.name(),
                "is_active": random.random() > 0.08,
                "ingested_at": _now(),
            }
        )
    return rows


def generate_crm_deals(fake: Faker, n: int, customer_ids: list[int]) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        rows.append(
            {
                "deal_id": i,
                "customer_id": random.choice(customer_ids),
                "deal_stage": random.choice(DEAL_STAGES),
                "amount": round(random.uniform(1_000, 250_000), 2),
                "currency": "USD",
                "close_date": fake.date_between(start_date="-1y", end_date="+90d"),
                "owner": fake.name(),
                "ingested_at": _now(),
            }
        )
    return rows


def generate_erp_invoices(fake: Faker, n: int, customer_ids: list[int]) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        invoice_date = fake.date_between(start_date="-1y", end_date="today")
        rows.append(
            {
                "invoice_id": i,
                "customer_id": random.choice(customer_ids),
                "invoice_date": invoice_date,
                "due_date": invoice_date + timedelta(days=30),
                "amount": round(random.uniform(500, 80_000), 2),
                "status": random.choice(INVOICE_STATUSES),
                "ingested_at": _now(),
            }
        )
    return rows


def generate_hris_employees(fake: Faker, n: int) -> list[dict]:
    rows = []
    for i in range(1, n + 1):
        rows.append(
            {
                "employee_id": i,
                "full_name": fake.name(),
                "email": fake.company_email(),
                "department": random.choice(DEPARTMENTS),
                "hire_date": fake.date_between(start_date="-6y", end_date="-10d"),
                "manager_id": random.choice([None] + list(range(1, max(2, i)))),
                "is_active": random.random() > 0.05,
                "ingested_at": _now(),
            }
        )
    return rows


def inject_schema_drift(con) -> None:
    """Simulate an upstream CRM system silently changing its schema."""
    print("[inject] simulating upstream CRM schema drift on raw.crm_customers")
    con.execute("ALTER TABLE raw.crm_customers RENAME COLUMN industry TO industry_segment")
    con.execute("ALTER TABLE raw.crm_customers DROP COLUMN is_active")
    con.execute("ALTER TABLE raw.crm_customers ADD COLUMN lifecycle_stage VARCHAR")


def inject_dq_issues(con) -> None:
    """Simulate realistic upstream data-quality problems."""
    print("[inject] simulating data-quality issues across raw tables")
    # Orphaned deals: customer_id that doesn't exist in crm_customers
    con.execute("""
        UPDATE raw.crm_deals SET customer_id = 999999
        WHERE deal_id IN (SELECT deal_id FROM raw.crm_deals USING SAMPLE 3 ROWS)
    """)
    # Null amounts (should never be null for a booked deal)
    con.execute("""
        UPDATE raw.crm_deals SET amount = NULL
        WHERE deal_id IN (SELECT deal_id FROM raw.crm_deals USING SAMPLE 5 ROWS)
    """)
    # Duplicate invoice_id (primary key violation upstream)
    con.execute("""
        INSERT INTO raw.erp_invoices
        SELECT * FROM raw.erp_invoices USING SAMPLE 2 ROWS
    """)
    # Negative invoice amounts
    con.execute("""
        UPDATE raw.erp_invoices SET amount = -amount
        WHERE invoice_id IN (SELECT invoice_id FROM raw.erp_invoices USING SAMPLE 2 ROWS)
    """)
    # Stale ingestion timestamp on HRIS (simulates a stalled connector)
    con.execute("""
        UPDATE raw.hris_employees SET ingested_at = ingested_at - INTERVAL '9 days'
    """)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--n-customers", type=int, default=400)
    parser.add_argument("--n-deals", type=int, default=1200)
    parser.add_argument("--n-invoices", type=int, default=900)
    parser.add_argument("--n-employees", type=int, default=150)
    parser.add_argument("--inject-schema-drift", action="store_true")
    parser.add_argument("--inject-dq-issues", action="store_true")
    args = parser.parse_args()

    random.seed(args.seed)
    fake = Faker()
    Faker.seed(args.seed)

    con = connect()

    customers = pd.DataFrame(generate_crm_customers(fake, args.n_customers))
    con.execute("CREATE OR REPLACE TABLE raw.crm_customers AS SELECT * FROM customers")

    customer_ids = customers["customer_id"].tolist()
    deals = pd.DataFrame(generate_crm_deals(fake, args.n_deals, customer_ids))
    con.execute("CREATE OR REPLACE TABLE raw.crm_deals AS SELECT * FROM deals")

    invoices = pd.DataFrame(generate_erp_invoices(fake, args.n_invoices, customer_ids))
    con.execute("CREATE OR REPLACE TABLE raw.erp_invoices AS SELECT * FROM invoices")

    employees = pd.DataFrame(generate_hris_employees(fake, args.n_employees))
    con.execute("CREATE OR REPLACE TABLE raw.hris_employees AS SELECT * FROM employees")

    if args.inject_schema_drift:
        inject_schema_drift(con)
    if args.inject_dq_issues:
        inject_dq_issues(con)

    print("Ingestion complete:")
    for table in ["crm_customers", "crm_deals", "erp_invoices", "hris_employees"]:
        count = con.execute(f"SELECT COUNT(*) FROM raw.{table}").fetchone()[0]
        print(f"  raw.{table}: {count} rows")

    con.close()


if __name__ == "__main__":
    main()

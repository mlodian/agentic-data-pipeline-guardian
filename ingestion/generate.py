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

import duckdb
import pandas as pd
from faker import Faker

from ingestion.db import connect, fetch_scalar

REGIONS = ["APAC", "EMEA", "NA", "LATAM"]
INDUSTRIES = ["Fintech", "Healthcare", "Retail", "Manufacturing", "SaaS", "Logistics"]
DEAL_STAGES = ["prospecting", "qualified", "proposal", "negotiation", "closed_won", "closed_lost"]
DEPARTMENTS = ["Engineering", "Sales", "Compliance", "Finance", "Operations", "HR"]
# "void" is deliberately absent: invoices here are raised against closed-won
# deals, so a void invoice means CRM and ERP disagree -- that is a finding,
# not a normal state. inject_reconciliation_break() is what creates them.
INVOICE_STATUSES = ["paid", "unpaid", "overdue"]


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


def generate_erp_invoices(fake: Faker, deals: list[dict], split_rate: float = 0.2) -> list[dict]:
    """Invoices are derived from closed-won CRM deals, not generated
    independently.

    This matters more than it looks. If the ERP invented its own customers
    and amounts, the two systems would never agree and cross-system
    reconciliation would be meaningless -- it would flag every run. By
    raising an invoice against each closed-won deal, clean data reconciles
    to the cent, so a reconciliation finding means something actually broke.

    About `split_rate` of deals are invoiced in two parts, because real
    billing does that, and it proves the reconciliation compares summed
    totals per customer rather than naively matching row for row.
    """
    rows = []
    invoice_id = 1
    for deal in deals:
        if deal["deal_stage"] != "closed_won" or deal["amount"] is None:
            continue

        if random.random() < split_rate:
            first = round(deal["amount"] / 2, 2)
            amounts = [first, round(deal["amount"] - first, 2)]
        else:
            amounts = [deal["amount"]]

        for amount in amounts:
            invoice_date = fake.date_between(start_date="-1y", end_date="today")
            rows.append(
                {
                    "invoice_id": invoice_id,
                    "deal_id": deal["deal_id"],
                    "customer_id": deal["customer_id"],
                    "invoice_date": invoice_date,
                    "due_date": invoice_date + timedelta(days=30),
                    "amount": amount,
                    "status": random.choice(INVOICE_STATUSES),
                    "ingested_at": _now(),
                }
            )
            invoice_id += 1
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


def inject_reconciliation_break(con) -> None:
    """Simulate the CRM and the ERP drifting apart.

    This is the failure class no single-table check can see: after this
    runs, every row in both tables is still individually valid. Deals are
    well-formed, invoices are well-formed, every key resolves. The systems
    simply disagree about how much money there is -- which is exactly the
    kind of problem that reaches a finance close before it reaches a
    data engineer.
    """
    print("[inject] simulating CRM/ERP reconciliation drift")
    # Invoices voided in the ERP while the deal stays closed-won in the CRM.
    con.execute("""
        UPDATE raw.erp_invoices SET status = 'void'
        WHERE invoice_id IN (SELECT invoice_id FROM raw.erp_invoices USING SAMPLE 12 ROWS)
    """)
    # A partial load: some invoices never landed at all.
    con.execute("""
        DELETE FROM raw.erp_invoices
        WHERE invoice_id IN (SELECT invoice_id FROM raw.erp_invoices USING SAMPLE 8 ROWS)
    """)
    # An amount edited in one system only.
    con.execute("""
        UPDATE raw.erp_invoices SET amount = round(amount * 0.85, 2)
        WHERE invoice_id IN (SELECT invoice_id FROM raw.erp_invoices USING SAMPLE 10 ROWS)
    """)


def inject_table_drop(con, table: str = "crm_customers") -> None:
    """Simulate an upstream system removing a table entirely.

    This is the worst schema event in the catalogue: not a renamed column, not
    a type change, but a table that downstream models join to simply ceasing
    to exist. crm_customers is the conformed dimension every mart depends on,
    so dropping it is both the highest-weighted finding and the widest blast
    radius the lineage graph can report.
    """
    print(f"[inject] simulating upstream removal of raw.{table}")
    con.execute(f"DROP TABLE IF EXISTS raw.{table}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--n-customers", type=int, default=400)
    parser.add_argument("--n-deals", type=int, default=1200)
    parser.add_argument("--n-employees", type=int, default=150)
    parser.add_argument("--inject-schema-drift", action="store_true")
    parser.add_argument("--inject-dq-issues", action="store_true")
    parser.add_argument("--inject-reconciliation-break", action="store_true")
    parser.add_argument("--inject-table-drop", action="store_true")
    parser.add_argument(
        "--scenario",
        choices=["clean", "critical"],
        default=None,
        help=(
            "Preset scenarios. 'critical' applies every injector at once -- a "
            "dropped upstream table, schema drift, data-quality faults and a "
            "CRM/ERP reconciliation break -- so the worst case is one command "
            "and reproduces identically for a given seed."
        ),
    )
    args = parser.parse_args()

    random.seed(args.seed)
    fake = Faker()
    Faker.seed(args.seed)

    con = connect()

    customers = pd.DataFrame(generate_crm_customers(fake, args.n_customers))
    con.execute("CREATE OR REPLACE TABLE raw.crm_customers AS SELECT * FROM customers")

    customer_ids = customers["customer_id"].tolist()
    deal_rows = generate_crm_deals(fake, args.n_deals, customer_ids)
    deals = pd.DataFrame(deal_rows)
    con.execute("CREATE OR REPLACE TABLE raw.crm_deals AS SELECT * FROM deals")

    # Invoices are raised against closed-won deals, so their count is derived
    # rather than configured -- see generate_erp_invoices.
    invoices = pd.DataFrame(generate_erp_invoices(fake, deal_rows))
    con.execute("CREATE OR REPLACE TABLE raw.erp_invoices AS SELECT * FROM invoices")

    employees = pd.DataFrame(generate_hris_employees(fake, args.n_employees))
    con.execute("CREATE OR REPLACE TABLE raw.hris_employees AS SELECT * FROM employees")

    if args.scenario == "critical":
        args.inject_schema_drift = True
        args.inject_dq_issues = True
        args.inject_reconciliation_break = True
        args.inject_table_drop = True

    if args.inject_schema_drift:
        inject_schema_drift(con)
    if args.inject_dq_issues:
        inject_dq_issues(con)
    if args.inject_reconciliation_break:
        inject_reconciliation_break(con)
    if args.inject_table_drop:
        inject_table_drop(con)

    print("Ingestion complete:")
    for table in ["crm_customers", "crm_deals", "erp_invoices", "hris_employees"]:
        # A table the drop-injector removed is gone on purpose -- report it as
        # dropped rather than raising while summarising what was just built.
        try:
            count = fetch_scalar(con, f"SELECT COUNT(*) FROM raw.{table}")
        except duckdb.Error:
            print(f"  raw.{table}: DROPPED (simulated upstream removal)")
            continue
        print(f"  raw.{table}: {count} rows")

    con.close()


if __name__ == "__main__":
    main()

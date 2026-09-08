"""Declarative check configuration: one place that says what "good" looks
like for each raw table. Adding a new upstream table means adding an entry
here, not writing new check code -- mirrors the risk-weighting/test-case
config approach used for the AML data-quality framework this project is
modeled on."""
from __future__ import annotations

SCHEMA_BASELINE_PATH = "agent/schema_baseline.json"
ROW_COUNT_BASELINE_PATH = "agent/row_count_baseline.json"

TABLE_CHECKS = {
    "raw.crm_customers": {
        "primary_key": "customer_id",
        "not_null": ["customer_id", "company_name"],
        "freshness": {"column": "ingested_at", "max_age_hours": 48},
    },
    "raw.crm_deals": {
        "primary_key": "deal_id",
        "not_null": ["deal_id", "customer_id", "amount"],
        "foreign_keys": [
            {"column": "customer_id", "ref_table": "raw.crm_customers", "ref_column": "customer_id"}
        ],
        "ranges": {"amount": {"min": 0}},
        "freshness": {"column": "ingested_at", "max_age_hours": 48},
    },
    "raw.erp_invoices": {
        "primary_key": "invoice_id",
        "not_null": ["invoice_id", "customer_id", "amount"],
        "foreign_keys": [
            {"column": "customer_id", "ref_table": "raw.crm_customers", "ref_column": "customer_id"}
        ],
        "ranges": {"amount": {"min": 0}},
        "freshness": {"column": "ingested_at", "max_age_hours": 48},
    },
    "raw.hris_employees": {
        "primary_key": "employee_id",
        "not_null": ["employee_id", "full_name", "email"],
        "freshness": {"column": "ingested_at", "max_age_hours": 48},
    },
}

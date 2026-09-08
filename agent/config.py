"""Declarative configuration: one place that says what "good" looks like for
each raw table, how much each kind of failure actually matters, how data
flows downstream, and which failures are safe to auto-remediate.

Everything here is data, not code. Adding a new upstream table means adding
an entry here, not writing new check logic. Keeping the rules declarative is
what lets a non-author audit what is actually being enforced without reading
the implementation.
"""
from __future__ import annotations

from typing import Any

SCHEMA_BASELINE_PATH = "agent/schema_baseline.json"
ROW_COUNT_BASELINE_PATH = "agent/row_count_baseline.json"

# ---------------------------------------------------------------------------
# Per-table check rules
# ---------------------------------------------------------------------------

TABLE_CHECKS: dict[str, dict[str, Any]] = {
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
        "not_null": ["invoice_id", "deal_id", "customer_id", "amount"],
        "foreign_keys": [
            {"column": "customer_id", "ref_table": "raw.crm_customers", "ref_column": "customer_id"},
            {"column": "deal_id", "ref_table": "raw.crm_deals", "ref_column": "deal_id"},
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

# ---------------------------------------------------------------------------
# Risk weighting
#
# A finding's risk is: check weight x severity multiplier x table criticality.
# The weights encode judgment that a fixed severity label can't: a dropped
# column and a stray PII-named column are both "findings", but one silently
# breaks every downstream model and the other is a governance to-do.
# ---------------------------------------------------------------------------

CHECK_RISK_WEIGHTS: dict[str, float] = {
    # Schema drift -- breaks contracts, usually silently
    "schema_dropped_table": 10.0,
    "schema_dropped_column": 9.0,
    "schema_type_changed": 8.0,
    "schema_new_table": 1.0,
    "schema_new_column": 1.0,
    # Integrity -- wrong numbers, not missing ones
    "uniqueness": 8.0,
    "referential_integrity": 7.0,
    "reconciliation_totals": 7.0,
    "reconciliation_coverage": 6.0,
    "not_null": 6.0,
    "value_range": 5.0,
    # Volume + timeliness
    "row_count_zscore": 5.0,
    "row_count_anomaly": 4.0,
    "freshness": 4.0,
    # Meta
    "health_degradation": 3.0,
    "pii_column_detected": 2.0,
}

DEFAULT_CHECK_RISK_WEIGHT = 5.0

SEVERITY_MULTIPLIERS: dict[str, float] = {
    "CRITICAL": 1.0,
    "WARNING": 0.4,
    "INFO": 0.1,
}

# How much each table matters. crm_customers is the conformed dimension every
# other model joins to, so the same failure there costs more than it does on
# a leaf table.
TABLE_CRITICALITY: dict[str, float] = {
    "raw.crm_customers": 1.5,
    "raw.crm_deals": 1.2,
    "raw.erp_invoices": 1.2,
    "raw.hris_employees": 0.8,
}

DEFAULT_TABLE_CRITICALITY = 1.0

# Health-score bands. Deliberately explicit rather than magic numbers buried
# in the scoring function.
HEALTH_BANDS: list[tuple[int, str]] = [
    (90, "HEALTHY"),
    (70, "DEGRADED"),
    (0, "UNHEALTHY"),
]

# A drop of this many points versus the previous run is itself a finding --
# absolute score can look acceptable while the trend is the real story.
HEALTH_DEGRADATION_THRESHOLD = 15.0

# ---------------------------------------------------------------------------
# Lineage: raw table -> staging model -> mart model
#
# Declared rather than inferred, so it is auditable and testable. It mirrors
# the real ref() graph in dbt_project/models/ -- tests/test_lineage.py asserts
# every node here corresponds to a real .sql file, so this can't silently rot.
# ---------------------------------------------------------------------------

LINEAGE_EDGES: dict[str, list[str]] = {
    "raw.crm_customers": ["stg_crm_customers"],
    "raw.crm_deals": ["stg_crm_deals"],
    "raw.erp_invoices": ["stg_erp_invoices"],
    "raw.hris_employees": ["stg_hris_employees"],
    "stg_crm_customers": ["dim_customers"],
    "stg_crm_deals": ["fct_deals"],
    "stg_erp_invoices": ["fct_invoices"],
    "stg_hris_employees": [],
    "dim_customers": ["customer_revenue_summary"],
    "fct_deals": ["customer_revenue_summary"],
    "fct_invoices": ["customer_revenue_summary"],
    "customer_revenue_summary": [],
}

# ---------------------------------------------------------------------------
# Cross-system reconciliation
#
# The checks that catch the failures no single-table test can see: CRM says
# the deal closed, ERP never invoiced it. Both tables pass every one of their
# own checks; the disagreement between them is the finding.
# ---------------------------------------------------------------------------

RECONCILIATION_RULES: list[dict[str, Any]] = [
    {
        "name": "closed_won_deals_vs_invoiced_revenue",
        "left_table": "raw.crm_deals",
        "left_filter": "deal_stage = 'closed_won'",
        "left_amount": "amount",
        "right_table": "raw.erp_invoices",
        "right_filter": "status <> 'void'",
        "right_amount": "amount",
        "group_by": "customer_id",
        # Invoices are raised against closed-won deals, so the two systems
        # should agree to the cent. 1% absorbs rounding on split invoices.
        "tolerance_pct": 0.01,
        "description": (
            "Revenue booked in the CRM as closed-won should equal revenue "
            "invoiced in the ERP for the same customer."
        ),
    }
]

# ---------------------------------------------------------------------------
# Quarantine rules: which findings describe rows that can be safely isolated
# out of the raw layer so downstream models stop consuming them.
#
# Every rule is a positive definition of a *bad* row. Nothing here deletes
# anything -- rows are copied into the quarantine schema, and the raw-layer
# delete only happens when GUARDIAN_ENABLE_QUARANTINE=true.
# ---------------------------------------------------------------------------

QUARANTINE_RULES: dict[str, dict[str, Any]] = {
    "value_range": {
        "predicate": "{column} < 0",
        "reason": "value below the configured minimum",
    },
    "not_null": {
        "predicate": "{column} is null",
        "reason": "required column is null",
    },
}

QUARANTINE_SCHEMA = "quarantine"

"""Tests for risk-weighted health scoring.

The score is a number people will make decisions on, so these tests pin down
the properties that make it trustworthy: it is ordered (worse data always
scores lower), it is weighted (not all findings cost the same), it is
bounded (never negative, never above 100), and it is reproducible by hand
from the published weights.
"""
import pytest

from agent.config import CHECK_RISK_WEIGHTS, SEVERITY_MULTIPLIERS, TABLE_CRITICALITY
from agent.scoring import (
    band_for,
    canonical_table,
    finding_risk,
    score_findings,
    table_scores,
)


def finding(check_name, severity="CRITICAL", table="raw.crm_deals", column=None):
    return {
        "check_name": check_name,
        "severity": severity,
        "table": table,
        "column": column,
        "message": "",
        "details": {},
    }


# --- table-name normalisation ---------------------------------------------

def test_canonical_table_qualifies_bare_names():
    """Schema-diff findings carry bare table names from information_schema
    while row-level checks carry 'raw.x'. Both must score identically or the
    same table would be weighted two different ways in one run."""
    assert canonical_table("crm_deals") == "raw.crm_deals"
    assert canonical_table("raw.crm_deals") == "raw.crm_deals"


def test_bare_and_qualified_names_score_identically():
    bare = finding("uniqueness", table="crm_deals")
    qualified = finding("uniqueness", table="raw.crm_deals")
    assert finding_risk(bare) == finding_risk(qualified)


# --- risk weighting --------------------------------------------------------

def test_risk_is_weight_times_severity_times_criticality():
    """The formula is published in the report, so it has to actually be the
    formula -- this recomputes it by hand from config."""
    f = finding("schema_dropped_column", severity="CRITICAL", table="raw.crm_customers")
    expected = (
        CHECK_RISK_WEIGHTS["schema_dropped_column"]
        * SEVERITY_MULTIPLIERS["CRITICAL"]
        * TABLE_CRITICALITY["raw.crm_customers"]
    )
    assert finding_risk(f) == pytest.approx(expected)


def test_critical_costs_more_than_warning_for_the_same_check():
    critical = finding("not_null", severity="CRITICAL")
    warning = finding("not_null", severity="WARNING")
    assert finding_risk(critical) > finding_risk(warning)


def test_same_failure_costs_more_on_a_more_critical_table():
    """crm_customers is the conformed dimension everything joins to, so an
    identical failure there has to outweigh one on a leaf table."""
    on_dimension = finding("uniqueness", table="raw.crm_customers")
    on_leaf = finding("uniqueness", table="raw.hris_employees")
    assert finding_risk(on_dimension) > finding_risk(on_leaf)


def test_unknown_check_falls_back_to_default_weight():
    """A new check added without a weight must still score, not crash."""
    assert finding_risk(finding("some_new_check_nobody_weighted")) > 0


# --- aggregate scoring -----------------------------------------------------

def test_no_findings_is_a_perfect_score():
    health = score_findings([])
    assert health.score == 100.0
    assert health.band == "HEALTHY"


def test_more_findings_lower_the_score_monotonically():
    one = score_findings([finding("not_null")]).score
    two = score_findings([finding("not_null"), finding("uniqueness")]).score
    assert two < one < 100.0


def test_score_is_floored_at_zero_not_negative():
    """Fifty critical findings shouldn't produce a score of -400; the floor
    is what keeps the number readable on a dashboard."""
    health = score_findings([finding("schema_dropped_table") for _ in range(50)])
    assert health.score == 0.0
    assert health.band == "UNHEALTHY"


def test_severity_counts_are_tallied():
    health = score_findings([
        finding("uniqueness", severity="CRITICAL"),
        finding("not_null", severity="WARNING"),
        finding("schema_new_column", severity="INFO"),
    ])
    assert health.by_severity == {"CRITICAL": 1, "WARNING": 1, "INFO": 1}


def test_top_contributors_are_ranked_by_risk_not_order():
    """The report leads with 'what is hurting most', so ranking has to be by
    risk, not by the order checks happened to run in."""
    health = score_findings([
        finding("pii_column_detected", severity="WARNING"),
        finding("schema_dropped_table", severity="CRITICAL"),
        finding("freshness", severity="WARNING"),
    ], top_n=2)
    assert health.top_contributors[0]["check_name"] == "schema_dropped_table"
    assert len(health.top_contributors) == 2


def test_risk_is_attributed_to_the_right_table():
    health = score_findings([
        finding("uniqueness", table="raw.crm_deals"),
        finding("uniqueness", table="raw.erp_invoices"),
    ])
    assert set(health.by_table) == {"raw.crm_deals", "raw.erp_invoices"}


# --- bands -----------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (100.0, "HEALTHY"), (90.0, "HEALTHY"),
    (89.9, "DEGRADED"), (70.0, "DEGRADED"),
    (69.9, "UNHEALTHY"), (0.0, "UNHEALTHY"),
])
def test_band_boundaries(score, expected):
    """Band edges are what trigger different human responses, so they're
    pinned explicitly rather than left to drift with the thresholds."""
    assert band_for(score) == expected


# --- per-table scores ------------------------------------------------------

def test_per_table_scores_isolate_one_bad_table():
    """A single rotten table must not be averaged into invisibility by
    healthy ones -- that's the whole reason per-table scores exist."""
    scores = table_scores([
        finding("schema_dropped_table", table="raw.crm_deals"),
        finding("schema_new_column", severity="INFO", table="raw.hris_employees"),
    ])
    assert scores["raw.crm_deals"] < 90
    assert scores["raw.hris_employees"] > 99

"""Tests for lineage and blast-radius analysis.

Two jobs here. First, the graph must answer impact questions correctly.
Second -- and this is the one that stops the feature rotting -- the declared
lineage must stay in sync with the dbt models that actually exist on disk.
"""
from pathlib import Path

from agent.config import LINEAGE_EDGES
from agent.lineage import (
    annotate_findings,
    blast_radius,
    downstream_of,
    impact_summary,
    upstream_of,
)

DBT_MODELS = Path("dbt_project/models")


def finding(table, severity="CRITICAL", check="uniqueness"):
    return {"check_name": check, "severity": severity, "table": table,
            "column": "customer_id", "message": "", "details": {}}


# --- the graph matches reality --------------------------------------------

def test_every_declared_model_exists_on_disk():
    """The lineage map is hand-declared, which is only safe if something
    fails loudly when a model is renamed or deleted. This is that something."""
    model_files = {p.stem for p in DBT_MODELS.rglob("*.sql")}
    declared_models = {
        node for node in LINEAGE_EDGES if not node.startswith("raw.")
    }
    missing = declared_models - model_files
    assert not missing, f"lineage references models that don't exist: {missing}"


def test_every_dbt_model_appears_in_the_lineage_map():
    """The reverse direction: a new model that nobody added to the map would
    silently have no blast radius, which is worse than a wrong one."""
    model_files = {p.stem for p in DBT_MODELS.rglob("*.sql")}
    declared = set(LINEAGE_EDGES)
    missing = model_files - declared
    assert not missing, f"models missing from the lineage map: {missing}"


# --- traversal -------------------------------------------------------------

def test_downstream_is_transitive_not_just_immediate():
    """A raw-table problem reaches the marts through staging; reporting only
    the immediate child would understate the damage."""
    assert downstream_of("raw.crm_customers") == [
        "stg_crm_customers", "dim_customers", "customer_revenue_summary",
    ]


def test_downstream_accepts_bare_table_names():
    """Schema-diff findings carry bare names, so lookups must normalise."""
    assert downstream_of("crm_customers") == downstream_of("raw.crm_customers")


def test_downstream_is_breadth_first_so_nearest_models_come_first():
    chain = downstream_of("raw.crm_deals")
    assert chain.index("stg_crm_deals") < chain.index("fct_deals")


def test_leaf_model_has_no_downstream():
    assert downstream_of("customer_revenue_summary") == []


def test_unknown_node_returns_empty_rather_than_raising():
    """An unmonitored table shouldn't take the whole run down."""
    assert downstream_of("raw.not_a_real_table") == []


def test_blast_radius_counts_all_affected_models():
    assert blast_radius("raw.crm_customers") == 3
    assert blast_radius("raw.hris_employees") == 1


def test_hris_has_a_smaller_blast_radius_than_crm():
    """This asymmetry is the point of lineage-aware triage: an identical
    failure on HRIS genuinely matters less than one on the CRM."""
    assert blast_radius("raw.hris_employees") < blast_radius("raw.crm_customers")


def test_upstream_finds_all_parents_of_a_join():
    assert upstream_of("customer_revenue_summary") == [
        "dim_customers", "fct_deals", "fct_invoices",
    ]


# --- annotation ------------------------------------------------------------

def test_annotate_adds_blast_radius_to_each_finding():
    annotated = annotate_findings([finding("raw.crm_customers")])
    assert annotated[0]["details"]["blast_radius"] == 3
    assert "customer_revenue_summary" in annotated[0]["details"]["downstream_models"]


def test_annotate_does_not_mutate_the_original_finding():
    """Findings get persisted to history as well as reported; annotation
    must not reach back and edit the record that was already collected."""
    original = finding("raw.crm_customers")
    annotate_findings([original])
    assert original["details"] == {}


def test_run_level_findings_are_passed_through_untouched():
    """A trend finding isn't about a table, so it has no blast radius."""
    trend = {"check_name": "health_degradation", "severity": "WARNING",
             "table": "__run__", "message": "", "details": {}}
    assert annotate_findings([trend])[0]["details"] == {}


# --- correlation -----------------------------------------------------------

def test_impact_summary_groups_findings_by_the_model_they_threaten():
    """This is the view that changes what a human opens first: separate
    findings on separate tables converging on one mart is one incident."""
    summary = impact_summary([
        finding("raw.crm_customers"),
        finding("raw.crm_deals"),
        finding("raw.erp_invoices"),
    ])
    top = summary["impacted_models"][0]
    assert top["model"] == "customer_revenue_summary"
    assert top["findings"] == 3
    assert len(top["sources"]) == 3


def test_impact_summary_ranks_critical_before_merely_numerous():
    summary = impact_summary([
        finding("raw.crm_customers", severity="CRITICAL"),
        finding("raw.hris_employees", severity="INFO"),
    ])
    models = [m["model"] for m in summary["impacted_models"]]
    assert models.index("customer_revenue_summary") < models.index("stg_hris_employees")


def test_impact_summary_is_empty_when_nothing_is_wrong():
    assert impact_summary([])["total_impacted"] == 0

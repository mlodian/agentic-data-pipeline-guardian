"""Integration tests for the Guardian LangGraph pipeline.

These exercise the whole graph -- collect -> score -> correlate -> reason ->
[remediate] -> [escalate] -> write_report -> persist -- end to end in
--dry-run mode against real seeded data, so they need no API key and no
network. What they're really protecting is the routing: which nodes run for
clean data, for dirty data, and for data that can be remediated.
"""
import sys

import ingestion.generate as generate
from agent.guardian_agent import (
    build_graph,
    route_after_reason,
    route_after_remediate,
)


def _seed(monkeypatch, seed: int, drift: bool = False, dq_issues: bool = False,
          reconciliation: bool = False):
    argv = ["generate", "--seed", str(seed), "--n-customers", "40",
            "--n-deals", "80", "--n-employees", "15"]
    if drift:
        argv.append("--inject-schema-drift")
    if dq_issues:
        argv.append("--inject-dq-issues")
    if reconciliation:
        argv.append("--inject-reconciliation-break")
    monkeypatch.setattr(sys, "argv", argv)
    generate.main()


def _run(tmp_path, monkeypatch, **seed_kwargs):
    monkeypatch.setenv("GUARDIAN_DB_PATH", str(tmp_path / "guardian.duckdb"))
    monkeypatch.delenv("GUARDIAN_OPEN_GH_ISSUES", raising=False)
    monkeypatch.delenv("GUARDIAN_ENABLE_QUARANTINE", raising=False)
    _seed(monkeypatch, **seed_kwargs)
    return build_graph().invoke({"dry_run": True})


# --- routing decisions in isolation ---------------------------------------

def test_route_prefers_remediation_when_rows_can_be_isolated():
    state = {"want_quarantine": True, "quarantine_plan": [{"table": "raw.x"}], "open_issue": True}
    assert route_after_reason(state) == "remediate"


def test_route_escalates_when_critical_but_nothing_to_quarantine():
    assert route_after_reason({"open_issue": True, "quarantine_plan": []}) == "escalate"


def test_route_goes_straight_to_the_report_when_all_is_well():
    assert route_after_reason({"open_issue": False}) == "write_report"


def test_quarantine_alone_does_not_suppress_escalation():
    """Isolating bad rows fixes the symptom, not the upstream cause -- a
    critical finding still has to reach a human afterwards."""
    assert route_after_remediate({"open_issue": True}) == "escalate"


def test_remediation_without_critical_findings_skips_paging_anyone():
    assert route_after_remediate({"open_issue": False}) == "write_report"


# --- clean data ------------------------------------------------------------

def test_clean_run_produces_no_critical_findings(tmp_path, monkeypatch):
    final = _run(tmp_path, monkeypatch, seed=1)
    assert final["report_path"]
    assert final["open_issue"] is False
    assert final.get("github_issue_url") is None
    assert [f for f in final["findings"] if f["severity"] == "CRITICAL"] == []


def test_clean_run_scores_in_the_healthy_band(tmp_path, monkeypatch):
    """PII governance warnings are expected even on pristine data, so
    'healthy' has to tolerate them rather than demand a perfect 100."""
    final = _run(tmp_path, monkeypatch, seed=1)
    assert final["health"]["band"] == "HEALTHY"
    assert final["health"]["score"] > 90


def test_clean_run_reconciles_the_two_systems(tmp_path, monkeypatch):
    """Invoices are generated from closed-won deals, so a clean run must
    produce no reconciliation findings -- otherwise the check is noise."""
    names = {f["check_name"] for f in _run(tmp_path, monkeypatch, seed=1)["findings"]}
    assert "reconciliation_totals" not in names
    assert "reconciliation_coverage" not in names


# --- dirty data ------------------------------------------------------------

def test_dirty_run_flags_critical_and_requests_escalation(tmp_path, monkeypatch):
    final = _run(tmp_path, monkeypatch, seed=2, dq_issues=True)
    assert [f for f in final["findings"] if f["severity"] == "CRITICAL"]
    assert final["open_issue"] is True


def test_dirty_run_scores_materially_lower_than_a_clean_one(tmp_path, monkeypatch):
    clean = _run(tmp_path / "a", monkeypatch, seed=1)["health"]["score"]
    dirty = _run(tmp_path / "b", monkeypatch, seed=2, dq_issues=True)["health"]["score"]
    assert dirty < clean


def test_reconciliation_break_is_caught_even_though_each_table_is_valid(tmp_path, monkeypatch):
    """The point of the cross-system tier: every row in both tables passes
    its own checks, and the systems still disagree."""
    final = _run(tmp_path, monkeypatch, seed=3, reconciliation=True)
    names = {f["check_name"] for f in final["findings"]}
    assert names & {"reconciliation_totals", "reconciliation_coverage"}


def test_findings_are_annotated_with_downstream_impact(tmp_path, monkeypatch):
    final = _run(tmp_path, monkeypatch, seed=2, dq_issues=True)
    table_findings = [f for f in final["findings"] if f["table"] != "__run__"]
    assert all("blast_radius" in f["details"] for f in table_findings)


def test_impact_summary_names_the_models_at_risk(tmp_path, monkeypatch):
    final = _run(tmp_path, monkeypatch, seed=2, dq_issues=True)
    models = {m["model"] for m in final["impact"]["impacted_models"]}
    assert "customer_revenue_summary" in models


# --- remediation path ------------------------------------------------------

def test_quarantine_is_planned_but_not_applied_without_opt_in(tmp_path, monkeypatch):
    """The graph reaches remediate_node and still writes nothing, because
    GUARDIAN_ENABLE_QUARANTINE is unset."""
    final = _run(tmp_path, monkeypatch, seed=2, dq_issues=True)
    result = final.get("quarantine_result")
    assert result is not None
    assert result["enabled"] is False
    assert result["applied_count"] == 0


def test_quarantine_plan_is_costed_before_the_decision_is_made(tmp_path, monkeypatch):
    """Regression test: the plan used to be rendered with zero counts because
    rows were counted after reasoning, so the report said '0 rows' while the
    run summary said 7. Counts must exist by the time reason_node runs."""
    final = _run(tmp_path, monkeypatch, seed=2, dq_issues=True)
    assert any(a["matched_rows"] > 0 for a in final["quarantine_plan"])
    assert "0 row(s) matching" not in final["report_markdown"]


def test_escalate_node_skips_issue_creation_without_opt_in(tmp_path, monkeypatch):
    final = _run(tmp_path, monkeypatch, seed=2, dq_issues=True)
    assert final["open_issue"] is True
    assert final["github_issue_url"] is None


# --- persistence -----------------------------------------------------------

def test_every_run_is_recorded_to_history(tmp_path, monkeypatch):
    """Without this the next run has no trend and no z-score baseline."""
    monkeypatch.setenv("GUARDIAN_DB_PATH", str(tmp_path / "guardian.duckdb"))
    _seed(monkeypatch, seed=1)
    app = build_graph()
    app.invoke({"dry_run": True})
    app.invoke({"dry_run": True})

    from agent.history import recent_runs
    from ingestion.db import connect
    con = connect()
    runs = recent_runs(con)
    con.close()
    assert len(runs) == 2


def test_report_leads_with_the_health_score(tmp_path, monkeypatch):
    """The number a human reads first has to be in the report, not only in
    the CLI output."""
    final = _run(tmp_path, monkeypatch, seed=2, dq_issues=True)
    assert "Health score:" in final["report_markdown"]


# --- trend -----------------------------------------------------------------

def test_trend_describes_this_run_not_the_previous_one(tmp_path, monkeypatch):
    """Regression test: the trend was summarised from past runs only, so
    after three clean runs a collapsed fourth reported '0.0 points above the
    mean'. The delta must be this run's score against the runs before it."""
    monkeypatch.setenv("GUARDIAN_DB_PATH", str(tmp_path / "guardian.duckdb"))
    app = build_graph()
    for _ in range(3):
        _seed(monkeypatch, seed=1)
        app.invoke({"dry_run": True})
    _seed(monkeypatch, seed=1, dq_issues=True)
    final = app.invoke({"dry_run": True})

    trend = final["trend"]
    assert trend["runs"] == 4
    assert trend["current_score"] == final["health"]["score"]
    assert trend["delta_vs_mean"] < 0
    assert "below the mean of the previous 3 run(s)" in final["report_markdown"]


# --- escalation floor ------------------------------------------------------

class _ReportOnlyModel:
    """Stands in for ChatAnthropic: drafts a report and never escalates."""

    def __init__(self, *args, **kwargs):
        pass

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        class _Response:
            tool_calls = [{
                "name": "draft_incident_report",
                "args": {"title": "t", "markdown_report": "# model report"},
            }]
        return _Response()


def test_critical_findings_escalate_even_if_the_model_does_not(monkeypatch):
    """Whether something is CRITICAL is decided by the deterministic checks.
    The model can add escalations, but forgetting to call open_github_issue
    must not stop a critical finding from reaching a human."""
    import langchain_anthropic

    from agent.guardian_agent import reason_node

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", _ReportOnlyModel)

    critical = reason_node({"findings": [], "has_critical": True})
    assert critical["report_markdown"] == "# model report"
    assert critical["open_issue"] is True

    quiet = reason_node({"findings": [], "has_critical": False})
    assert quiet["open_issue"] is False

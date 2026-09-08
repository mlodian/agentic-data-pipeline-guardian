"""Integration tests for the Guardian LangGraph pipeline. These exercise the
whole graph (collect -> reason -> [escalate] -> write_report) end to end in
--dry-run mode against real seeded data, so they don't need an Anthropic API
key or network access."""
import sys

import ingestion.generate as generate
from agent.guardian_agent import build_graph


def _seed(monkeypatch, seed: int, drift: bool = False, dq_issues: bool = False):
    argv = ["generate", "--seed", str(seed), "--n-customers", "40",
            "--n-deals", "80", "--n-invoices", "60", "--n-employees", "15"]
    if drift:
        argv.append("--inject-schema-drift")
    if dq_issues:
        argv.append("--inject-dq-issues")
    monkeypatch.setattr(sys, "argv", argv)
    generate.main()


def test_clean_run_produces_no_critical_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDIAN_DB_PATH", str(tmp_path / "guardian.duckdb"))
    _seed(monkeypatch, seed=1)

    app = build_graph()
    final_state = app.invoke({"dry_run": True})

    assert final_state["report_path"]
    assert final_state["open_issue"] is False
    assert final_state.get("github_issue_url") is None
    critical = [f for f in final_state["findings"] if f["severity"] == "CRITICAL"]
    assert critical == []


def test_dirty_run_flags_critical_and_requests_escalation(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDIAN_DB_PATH", str(tmp_path / "guardian.duckdb"))
    # No committed baseline exists for this fresh temp DB, so schema-drift
    # comparison is skipped -- but referential-integrity, uniqueness, and
    # range checks all run off the live data regardless, and the injector
    # guarantees at least a duplicate PK and an orphaned FK.
    _seed(monkeypatch, seed=2, dq_issues=True)

    app = build_graph()
    final_state = app.invoke({"dry_run": True})

    critical = [f for f in final_state["findings"] if f["severity"] == "CRITICAL"]
    assert critical, "expected at least one CRITICAL finding from injected DQ issues"
    assert final_state["open_issue"] is True


def test_escalate_node_skips_issue_creation_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("GUARDIAN_DB_PATH", str(tmp_path / "guardian.duckdb"))
    monkeypatch.delenv("GUARDIAN_OPEN_GH_ISSUES", raising=False)
    _seed(monkeypatch, seed=2, dq_issues=True)

    app = build_graph()
    final_state = app.invoke({"dry_run": True})

    # Critical findings exist and open_issue was requested, but without the
    # explicit opt-in env var no real `gh issue create` call should happen.
    assert final_state["open_issue"] is True
    assert final_state["github_issue_url"] is None

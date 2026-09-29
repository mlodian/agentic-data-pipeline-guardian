"""The Guardian agent: a LangGraph state machine that turns deterministic
check findings into a scored, lineage-aware, triaged incident report -- and,
depending on what it finds, quarantines bad rows and opens a GitHub issue.

The pipeline is eight nodes:

    collect -> score -> correlate -> reason -> [remediate] -> [escalate]
            -> write_report -> persist -> END

Design intent: exactly ONE of those nodes calls an LLM (`reason_node`).
Detection (collect), quantification (score), impact analysis (correlate),
data actions (remediate), alerting (escalate), and persistence (persist) are
all plain, testable Python. The model's job is narrow and well-scoped --
synthesize findings into a report, and make two bounded decisions (escalate?
quarantine?) -- not to "figure out" whether the data is healthy.

That split is what makes this reliable enough to run unattended on a
schedule (see .github/workflows/guardian.yml) instead of just being a chat
demo: with no API key at all, every node except reason_node behaves
identically, and reason_node falls back to a deterministic report.

Usage:
    python -m agent.guardian_agent              # uses Claude if ANTHROPIC_API_KEY is set
    python -m agent.guardian_agent --dry-run     # always uses the deterministic fallback
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

from agent.config import TABLE_CHECKS
from agent.history import (
    RunRecord,
    check_health_degradation,
    historical_row_counts,
    new_run_id,
    record_run,
    recent_runs,
    summarize_trend,
)
from agent.lineage import annotate_findings, impact_summary
from agent.remediation import execute_quarantine, plan_quarantine, quarantine_enabled
from agent.run_checks import current_row_counts, run_all_checks
from agent.scoring import score_findings, table_scores
from ingestion.db import connect

load_dotenv()


class GuardianState(TypedDict, total=False):
    run_id: str
    findings: list[dict]
    row_counts: dict
    health: dict
    table_scores: dict
    trend: dict
    impact: dict
    has_critical: bool
    quarantine_plan: list[dict]
    quarantine_result: dict
    report_markdown: str
    report_path: str
    open_issue: bool
    issue_title: str
    issue_body: str
    github_issue_url: str | None
    want_quarantine: bool
    dry_run: bool


# ---------------------------------------------------------------------------
# Deterministic report rendering (also the fallback when no API key is set)
# ---------------------------------------------------------------------------

def _render_report(state: GuardianState) -> str:
    health = state.get("health", {})
    findings = state.get("findings", [])
    impact = state.get("impact", {})
    trend = state.get("trend", {})
    quarantine = state.get("quarantine_plan", [])

    lines = ["# Guardian Report (deterministic mode)", ""]
    lines.append(
        f"**Health score:** {health.get('score', 100.0)} / 100 "
        f"({health.get('band', 'HEALTHY')})"
    )
    if trend.get("runs", 0) > 1:
        delta = trend.get("delta_vs_mean")
        if delta is not None:
            direction = "above" if delta >= 0 else "below"
            lines.append(
                f"**Trend:** {abs(delta):.1f} points {direction} the mean of the "
                f"previous {trend['runs'] - 1} run(s)."
            )
    lines.append("")

    if not findings:
        lines.append("No issues were found. All schema, data-quality, and "
                     "cross-system reconciliation checks passed.")
        return "\n".join(lines) + "\n"

    by_severity = health.get("by_severity", {})
    lines.append(
        f"**{len(findings)} finding(s):** {by_severity.get('CRITICAL', 0)} critical, "
        f"{by_severity.get('WARNING', 0)} warning, {by_severity.get('INFO', 0)} info."
    )
    lines.append("")

    for severity in ("CRITICAL", "WARNING", "INFO"):
        group = [f for f in findings if f.get("severity") == severity]
        if not group:
            continue
        lines.append(f"## {severity}")
        lines.append("")
        for f in group:
            col = f".{f['column']}" if f.get("column") else ""
            radius = f.get("details", {}).get("blast_radius")
            suffix = f" _(affects {radius} downstream model(s))_" if radius else ""
            lines.append(f"- **{f['table']}{col}** -- {f['message']}{suffix}")
        lines.append("")

    impacted = impact.get("impacted_models", [])
    if impacted:
        lines.append("## Downstream impact")
        lines.append("")
        for entry in impacted[:5]:
            lines.append(
                f"- `{entry['model']}` -- {entry['findings']} upstream finding(s), "
                f"{entry['critical']} critical, from {', '.join(entry['sources'])}"
            )
        lines.append("")

    if quarantine:
        lines.append("## Quarantine plan")
        lines.append("")
        mode = "APPLIED" if quarantine_enabled() else "DRY RUN (set GUARDIAN_ENABLE_QUARANTINE=true to apply)"
        lines.append(f"_{mode}_")
        lines.append("")
        for action in quarantine:
            lines.append(
                f"- `{action['table']}.{action['column']}` -- "
                f"{action['matched_rows']} row(s) matching `{action['predicate']}` "
                f"({action['reason']})"
            )
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def collect_node(state: GuardianState) -> GuardianState:
    """Run every deterministic check against the warehouse. Nothing is
    interpreted here -- this node only gathers facts."""
    con = connect(read_only=False)
    history = {table: historical_row_counts(con, table) for table in TABLE_CHECKS}
    findings = [f.to_dict() for f in run_all_checks(con, history_counts=history)]
    row_counts = current_row_counts(con)
    con.close()

    return {
        **state,
        "run_id": state.get("run_id") or new_run_id(),
        "findings": findings,
        "row_counts": row_counts,
        "has_critical": any(f["severity"] == "CRITICAL" for f in findings),
    }


def score_node(state: GuardianState) -> GuardianState:
    """Turn findings into a risk-weighted 0-100 health score, and compare it
    against history.

    The degradation check runs *after* scoring and its finding is appended
    without re-scoring, on purpose: the score describes the state of the
    data, the trend finding describes the direction. Folding the trend back
    into the score would make the number depend on itself.
    """
    findings = state.get("findings", [])
    health = score_findings(findings)

    con = connect(read_only=False)
    previous = recent_runs(con, limit=5)
    con.close()

    degradation = check_health_degradation(
        health.score, previous[0] if previous else None
    )
    if degradation:
        findings = findings + [degradation.to_dict()]

    # summarize_trend expects newest-first *including* the run being judged.
    # `previous` holds only past runs, so passing it alone made the report
    # describe the last run's trend as this one's.
    current = RunRecord(
        run_id=state.get("run_id", ""),
        run_at=datetime.now(timezone.utc),
        health_score=health.score,
        band=health.band,
        total_findings=len(findings),
        critical_count=health.by_severity.get("CRITICAL", 0),
    )
    trend = summarize_trend([current, *previous])

    return {
        **state,
        "findings": findings,
        "health": health.to_dict(),
        "table_scores": table_scores(findings),
        "trend": trend,
        "has_critical": any(f["severity"] == "CRITICAL" for f in findings),
    }


def correlate_node(state: GuardianState) -> GuardianState:
    """Annotate every finding with what it breaks downstream, and roll the
    findings up by impacted model. Five findings that all land on the same
    mart are one incident, not five."""
    findings = annotate_findings(state.get("findings", []))

    # Cost the quarantine plan here, not in remediate_node. Counting matched
    # rows is a read-only query, and doing it before reason_node means both
    # Claude and the deterministic renderer decide whether to quarantine
    # while knowing exactly how many rows the action would move -- rather
    # than authorising an action whose size is only discovered afterwards.
    actions = plan_quarantine(findings)
    if actions:
        con = connect(read_only=True)
        costed = execute_quarantine(con, actions, state.get("run_id", "unknown"), enabled=False)
        con.close()
        actions = costed.actions

    return {
        **state,
        "findings": findings,
        "impact": impact_summary(findings),
        "quarantine_plan": [a.to_dict() for a in actions],
    }


def reason_node(state: GuardianState) -> GuardianState:
    """Ask Claude to synthesize everything above into a report a human can
    act on, and to make two bounded decisions: escalate, and quarantine.

    Falls back to a deterministic summary when no API key is configured (or
    --dry-run is passed), so the graph is fully runnable without one --
    which is what CI actually exercises.
    """
    findings = state.get("findings", [])
    eligible_quarantine = bool(state.get("quarantine_plan"))

    if state.get("dry_run") or not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            **state,
            "report_markdown": _render_report(state),
            "open_issue": state.get("has_critical", False),
            "want_quarantine": eligible_quarantine,
            "issue_title": "Guardian: critical data-quality findings detected",
            "issue_body": _render_report(state),
        }

    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.tools import tool

    captured: dict = {}

    @tool
    def draft_incident_report(title: str, markdown_report: str) -> str:
        """Save the incident report. `markdown_report` must be complete,
        well-formatted Markdown: lead with the health score and what changed
        since the last run, group findings by severity, name the downstream
        models at risk, and give a concrete next step per CRITICAL/WARNING
        item. Always call this exactly once."""
        captured["report_title"] = title
        captured["report_markdown"] = markdown_report
        return "Report captured."

    @tool
    def open_github_issue(title: str, body: str) -> str:
        """Open a GitHub issue for this run. Call this ONLY if at least one
        finding has severity CRITICAL -- routine WARNING/INFO findings
        should NOT page anyone."""
        captured["open_issue"] = True
        captured["issue_title"] = title
        captured["issue_body"] = body
        return "Issue queued for creation."

    @tool
    def request_quarantine(rationale: str) -> str:
        """Isolate the rows named in the quarantine plan out of the raw layer
        so downstream models stop consuming them. Call this ONLY when the
        plan's rows are unambiguously invalid (a negative invoice amount, a
        null required key) rather than merely suspicious, and explain why in
        `rationale`. Rows are copied to the quarantine schema first and are
        always recoverable."""
        captured["want_quarantine"] = True
        captured["quarantine_rationale"] = rationale
        return "Quarantine requested."

    model_name = os.environ.get("GUARDIAN_MODEL", "claude-sonnet-4-5-20250929")
    model = ChatAnthropic(model=model_name, temperature=0, max_tokens=4096)
    tools = [draft_incident_report, open_github_issue]
    if eligible_quarantine:
        tools.append(request_quarantine)
    model_with_tools = model.bind_tools(tools)

    system_prompt = (
        "You are the Guardian agent monitoring a data warehouse. You receive "
        "structured findings from deterministic schema-drift, data-quality and "
        "cross-system reconciliation checks, a risk-weighted health score, the "
        "score's recent history, and a lineage-derived list of the downstream "
        "models each finding threatens.\n\n"
        "You do not decide whether something is wrong -- that is already "
        "decided. Your job is triage and communication: what matters most, "
        "what is likely causing it, and what a data engineer should do first.\n\n"
        "Always call draft_incident_report exactly once. Lead with the score "
        "and the trend, group by severity, name the downstream models at risk, "
        "and give a concrete next step per issue. Be concise -- synthesize, "
        "don't restate every finding.\n\n"
        "Call open_github_issue only if at least one finding is CRITICAL.\n"
        "Call request_quarantine only if the quarantine plan's rows are "
        "unambiguously invalid rather than merely unusual."
    )
    user_prompt = json.dumps(
        {
            "health": state.get("health", {}),
            "trend": state.get("trend", {}),
            "table_scores": state.get("table_scores", {}),
            "downstream_impact": state.get("impact", {}),
            "quarantine_plan": state.get("quarantine_plan", []),
            "findings": findings,
        },
        indent=2,
        default=str,
    )

    try:
        response = model_with_tools.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        for call in getattr(response, "tool_calls", None) or []:
            if call["name"] == "draft_incident_report":
                draft_incident_report.invoke(call["args"])
            elif call["name"] == "open_github_issue":
                open_github_issue.invoke(call["args"])
            elif call["name"] == "request_quarantine":
                request_quarantine.invoke(call["args"])
    except Exception as exc:
        # The SDK already retries transient errors (see ChatAnthropic's
        # max_retries); if we get here the call failed anyway (outage,
        # invalid key, etc). Falling back keeps the promise that this
        # pipeline is always runnable -- a Claude outage should degrade the
        # report, not take down the whole scheduled run.
        print(f"[guardian] Claude call failed, falling back to deterministic report: {exc}")
        return {
            **state,
            "report_markdown": _render_report(state),
            "open_issue": state.get("has_critical", False),
            "want_quarantine": eligible_quarantine,
            "issue_title": "Guardian: critical data-quality findings detected",
            "issue_body": _render_report(state),
        }

    if "report_markdown" not in captured:
        # The model answered without calling the required tool. Rather than
        # ship an empty report, fall back -- the deterministic renderer is
        # always available and always correct.
        print("[guardian] model did not call draft_incident_report; using deterministic report.")
        captured["report_markdown"] = _render_report(state)

    # The model may escalate more than the rules require, never less: a
    # CRITICAL finding pages a human even if the model forgot to call
    # open_github_issue. Whether something is critical was decided by the
    # deterministic checks, not by the model.
    return {
        **state,
        "report_markdown": captured["report_markdown"],
        "open_issue": captured.get("open_issue", False) or state.get("has_critical", False),
        "want_quarantine": captured.get("want_quarantine", False),
        "issue_title": captured.get("issue_title", "Guardian: data-quality findings"),
        "issue_body": captured.get("issue_body", captured["report_markdown"]),
    }


def remediate_node(state: GuardianState) -> GuardianState:
    """Execute the quarantine plan. Gated twice: the plan only ever contains
    rules declared in config, and nothing is written unless
    GUARDIAN_ENABLE_QUARANTINE=true -- otherwise this reports what would
    have moved."""
    from agent.remediation import QuarantineAction

    actions = [
        QuarantineAction(
            table=a["table"],
            column=a["column"],
            check_name=a["check_name"],
            predicate=a["predicate"],
            reason=a["reason"],
        )
        for a in state.get("quarantine_plan", [])
    ]
    con = connect(read_only=False)
    result = execute_quarantine(con, actions, state.get("run_id", "unknown"))
    con.close()

    if result.enabled:
        print(f"[guardian] quarantined {result.total_rows} row(s) across "
              f"{result.applied_count} action(s).")
    else:
        print(f"[guardian] quarantine is disabled; {result.total_rows} row(s) "
              "would have been isolated. Set GUARDIAN_ENABLE_QUARANTINE=true to apply.")

    return {
        **state,
        "quarantine_result": result.to_dict(),
        "quarantine_plan": [a.to_dict() for a in result.actions],
    }


def escalate_node(state: GuardianState) -> GuardianState:
    """Open a GitHub issue via the gh CLI. Gated behind GUARDIAN_OPEN_GH_ISSUES
    so running the agent locally never spams a real issue tracker unless you
    opt in explicitly."""
    if os.environ.get("GUARDIAN_OPEN_GH_ISSUES", "false").lower() != "true":
        print(
            "[guardian] critical findings detected, but GUARDIAN_OPEN_GH_ISSUES "
            "is not 'true' -- skipping GitHub issue creation."
        )
        return {**state, "github_issue_url": None}

    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--title", state["issue_title"],
                "--body", state["issue_body"],
            ],
            capture_output=True, text=True, check=True,
        )
        url = result.stdout.strip()
        print(f"[guardian] opened GitHub issue: {url}")
        return {**state, "github_issue_url": url}
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"[guardian] failed to open GitHub issue: {detail}")
        return {**state, "github_issue_url": None}


def write_report_node(state: GuardianState) -> GuardianState:
    reports_dir = Path("docs/incident_reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = reports_dir / f"{timestamp}.md"
    report_path.write_text(state["report_markdown"])
    return {**state, "report_path": str(report_path)}


def persist_node(state: GuardianState) -> GuardianState:
    """Record this run into warehouse history so the next run has a trend and
    a row-count distribution to compare against. This is the node that makes
    the whole thing cumulative rather than amnesiac."""
    con = connect(read_only=False)
    record_run(
        con,
        run_id=state.get("run_id", new_run_id()),
        health=state.get("health", {}),
        findings=state.get("findings", []),
        row_counts=state.get("row_counts", {}),
    )
    con.close()
    return state


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_after_reason(state: GuardianState) -> str:
    if state.get("want_quarantine") and state.get("quarantine_plan"):
        return "remediate"
    if state.get("open_issue"):
        return "escalate"
    return "write_report"


def route_after_remediate(state: GuardianState) -> str:
    return "escalate" if state.get("open_issue") else "write_report"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(GuardianState)
    graph.add_node("collect", collect_node)
    graph.add_node("score", score_node)
    graph.add_node("correlate", correlate_node)
    graph.add_node("reason", reason_node)
    graph.add_node("remediate", remediate_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("write_report", write_report_node)
    graph.add_node("persist", persist_node)

    graph.set_entry_point("collect")
    graph.add_edge("collect", "score")
    graph.add_edge("score", "correlate")
    graph.add_edge("correlate", "reason")
    graph.add_conditional_edges(
        "reason",
        route_after_reason,
        {"remediate": "remediate", "escalate": "escalate", "write_report": "write_report"},
    )
    graph.add_conditional_edges(
        "remediate",
        route_after_remediate,
        {"escalate": "escalate", "write_report": "write_report"},
    )
    graph.add_edge("escalate", "write_report")
    graph.add_edge("write_report", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip the Claude call and use a deterministic summary instead.",
    )
    args = parser.parse_args()

    app = build_graph()
    final_state = app.invoke({"dry_run": args.dry_run})

    health = final_state.get("health", {})
    print(f"\nHealth score: {health.get('score')} ({health.get('band')})")
    print(f"Findings: {len(final_state.get('findings', []))}")
    impacted = final_state.get("impact", {}).get("total_impacted", 0)
    print(f"Downstream models impacted: {impacted}")
    quarantine = final_state.get("quarantine_result")
    if quarantine:
        verb = "quarantined" if quarantine["enabled"] else "eligible for quarantine"
        print(f"Rows {verb}: {quarantine['total_rows']}")
    print(f"Report written to: {final_state.get('report_path')}")
    if final_state.get("github_issue_url"):
        print(f"GitHub issue: {final_state['github_issue_url']}")


if __name__ == "__main__":
    main()

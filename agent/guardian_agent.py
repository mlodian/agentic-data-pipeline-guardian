"""The Guardian agent: a LangGraph state machine that turns deterministic
check findings into a triaged, human-readable incident report -- and, for
CRITICAL findings, opens a GitHub issue.

Design intent: only ONE node in this graph calls an LLM (`reason_node`).
Everything upstream (schema/DQ checks) and downstream (writing files,
shelling out to `gh`) is plain, testable Python. The model's job is
narrow and well-scoped -- synthesize findings into a report and make one
escalation decision -- not to "figure out" whether the data is healthy.
That split is deliberate: it's what makes this reliable enough to run
unattended on a schedule (see .github/workflows/guardian.yml) instead of
just being a chat demo.

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

from agent.run_checks import run_all_checks
from ingestion.db import connect

load_dotenv()


class GuardianState(TypedDict, total=False):
    findings: list[dict]
    has_critical: bool
    report_markdown: str
    report_path: str
    open_issue: bool
    issue_title: str
    issue_body: str
    github_issue_url: str | None
    dry_run: bool


def _build_findings_summary(findings: list[dict]) -> str:
    if not findings:
        return "No issues were found. All schema and data-quality checks passed."
    lines = []
    for f in findings:
        col = f".{f['column']}" if f.get("column") else ""
        lines.append(f"- [{f['severity']}] {f['table']}{col}: {f['message']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def collect_node(state: GuardianState) -> GuardianState:
    """Run every deterministic check (agent/checks.py) against the
    warehouse and hand the results to the reasoning step."""
    con = connect(read_only=True)
    findings = [f.to_dict() for f in run_all_checks(con)]
    con.close()
    has_critical = any(f["severity"] == "CRITICAL" for f in findings)
    return {**state, "findings": findings, "has_critical": has_critical}


def reason_node(state: GuardianState) -> GuardianState:
    """Ask Claude to synthesize findings into a report and decide whether
    to escalate. Falls back to a deterministic summary when no API key is
    configured (or --dry-run is passed), so the graph is fully runnable
    without one -- useful for CI and for trying this repo out before
    wiring up billing."""
    findings = state.get("findings", [])

    if state.get("dry_run") or not os.environ.get("ANTHROPIC_API_KEY"):
        report = _build_findings_summary(findings)
        return {
            **state,
            "report_markdown": f"# Guardian Report (deterministic mode)\n\n{report}\n",
            "open_issue": state.get("has_critical", False),
            "issue_title": "Guardian: critical data-quality findings detected",
            "issue_body": report,
        }

    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_core.tools import tool

    captured: dict = {}

    @tool
    def draft_incident_report(title: str, markdown_report: str) -> str:
        """Save the incident report. `markdown_report` must be complete,
        well-formatted Markdown explaining what was found, grouped by
        severity, with likely root cause and a recommended next step for
        each CRITICAL/WARNING item. Always call this exactly once."""
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

    model_name = os.environ.get("GUARDIAN_MODEL", "claude-sonnet-4-5-20250929")
    model = ChatAnthropic(model=model_name, temperature=0)
    model_with_tools = model.bind_tools([draft_incident_report, open_github_issue])

    system_prompt = (
        "You are the Guardian agent monitoring a data warehouse. You receive "
        "structured findings from deterministic schema-drift and data-quality "
        "checks (you do not decide whether something is wrong -- that's "
        "already decided; your job is triage and communication).\n\n"
        "Always call draft_incident_report exactly once to produce a report "
        "a data engineer can act on immediately: group by severity, explain "
        "the likely root cause in plain language, and suggest a concrete "
        "next step per issue. Be concise -- synthesize, don't just restate "
        "each finding.\n\n"
        "Call open_github_issue only if at least one finding is CRITICAL."
    )
    user_prompt = f"Findings from this run ({len(findings)} total):\n\n{json.dumps(findings, indent=2)}"

    response = model_with_tools.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    )

    for call in getattr(response, "tool_calls", None) or []:
        if call["name"] == "draft_incident_report":
            draft_incident_report.invoke(call["args"])
        elif call["name"] == "open_github_issue":
            open_github_issue.invoke(call["args"])

    if "report_markdown" not in captured:
        captured["report_markdown"] = f"# Guardian Report\n\n{_build_findings_summary(findings)}\n"

    return {
        **state,
        "report_markdown": captured["report_markdown"],
        "open_issue": captured.get("open_issue", False),
        "issue_title": captured.get("issue_title", "Guardian: data-quality findings"),
        "issue_body": captured.get("issue_body", captured["report_markdown"]),
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


def route_after_reason(state: GuardianState) -> str:
    return "escalate" if state.get("open_issue") else "write_report"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(GuardianState)
    graph.add_node("collect", collect_node)
    graph.add_node("reason", reason_node)
    graph.add_node("escalate", escalate_node)
    graph.add_node("write_report", write_report_node)

    graph.set_entry_point("collect")
    graph.add_edge("collect", "reason")
    graph.add_conditional_edges(
        "reason",
        route_after_reason,
        {"escalate": "escalate", "write_report": "write_report"},
    )
    graph.add_edge("escalate", "write_report")
    graph.add_edge("write_report", END)
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

    print(f"\nFindings: {len(final_state.get('findings', []))}")
    print(f"Report written to: {final_state.get('report_path')}")
    if final_state.get("github_issue_url"):
        print(f"GitHub issue: {final_state['github_issue_url']}")


if __name__ == "__main__":
    main()

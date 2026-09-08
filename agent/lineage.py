"""Lineage and blast radius.

A finding that says "raw.crm_customers.customer_id is not unique" is only
half the story. The half an on-call engineer actually needs is which
downstream models are now serving wrong numbers because of it.

The lineage graph is declared in agent/config.py rather than inferred at
runtime -- it is small, it is auditable, and tests/test_lineage.py asserts
that every node in it corresponds to a real .sql file in dbt_project/, so
it cannot silently drift away from the models it claims to describe.
"""
from __future__ import annotations

from collections import deque

from agent.config import LINEAGE_EDGES
from agent.scoring import canonical_table


def downstream_of(node: str) -> list[str]:
    """Every model transitively downstream of a node, breadth-first, so the
    nearest-affected models are listed first."""
    start = node if node in LINEAGE_EDGES else canonical_table(node)
    if start not in LINEAGE_EDGES:
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    queue = deque(LINEAGE_EDGES.get(start, []))
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        queue.extend(LINEAGE_EDGES.get(current, []))
    return ordered


def blast_radius(node: str) -> int:
    """How many downstream models a problem at this node can reach."""
    return len(downstream_of(node))


def upstream_of(node: str) -> list[str]:
    """Reverse lookup -- which nodes feed this one. Used to answer 'where did
    this actually come from' when a mart model is the thing that looks wrong."""
    target = node if node in LINEAGE_EDGES else canonical_table(node)
    return sorted(
        parent for parent, children in LINEAGE_EDGES.items() if target in children
    )


def annotate_findings(findings: list[dict]) -> list[dict]:
    """Attach downstream impact to each finding, in place of making the
    reader go look it up. Findings on '__run__' (trend-level, not
    table-level) are passed through untouched."""
    annotated = []
    for finding in findings:
        table = finding.get("table", "")
        enriched = {**finding, "details": {**finding.get("details", {})}}
        if table and table != "__run__":
            impacted = downstream_of(table)
            enriched["details"]["downstream_models"] = impacted
            enriched["details"]["blast_radius"] = len(impacted)
        annotated.append(enriched)
    return annotated


def impact_summary(findings: list[dict]) -> dict:
    """Roll findings up by the downstream model they threaten.

    This is the view that changes what a human does first: five findings
    across three raw tables can all land on the same mart, which makes that
    mart -- not any individual finding -- the thing to go look at.
    """
    per_model: dict[str, dict] = {}
    for finding in findings:
        table = finding.get("table", "")
        if not table or table == "__run__":
            continue
        severity = finding.get("severity", "INFO")
        for model in downstream_of(table):
            entry = per_model.setdefault(
                model, {"model": model, "findings": 0, "critical": 0, "sources": set()}
            )
            entry["findings"] += 1
            entry["sources"].add(canonical_table(table))
            if severity == "CRITICAL":
                entry["critical"] += 1

    summary = []
    for entry in per_model.values():
        summary.append(
            {
                "model": entry["model"],
                "findings": entry["findings"],
                "critical": entry["critical"],
                "sources": sorted(entry["sources"]),
            }
        )
    summary.sort(key=lambda e: (-e["critical"], -e["findings"], e["model"]))
    return {"impacted_models": summary, "total_impacted": len(summary)}

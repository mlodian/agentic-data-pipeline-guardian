"""Risk-weighted data-quality health scoring.

A flat list of findings answers "what is wrong". It does not answer the
question anyone actually asks next -- "how bad is it, and is it getting
worse". This module turns findings into a single auditable 0-100 score,
plus a per-table breakdown, using the weights declared in agent/config.py.

The scoring is deliberately linear and inspectable rather than clever: any
number this produces can be traced back to the exact findings and weights
that produced it, which is what makes it usable as a compliance-style
signal instead of a vibe.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agent.config import (
    CHECK_RISK_WEIGHTS,
    DEFAULT_CHECK_RISK_WEIGHT,
    DEFAULT_TABLE_CRITICALITY,
    HEALTH_BANDS,
    SEVERITY_MULTIPLIERS,
    TABLE_CRITICALITY,
)


def canonical_table(table: str) -> str:
    """Findings arrive with table names in two shapes: row-level checks
    query 'raw.crm_deals', while schema-diff and PII checks work off an
    information_schema snapshot keyed on the bare 'crm_deals'. Scoring and
    lineage both need one canonical spelling, so everything is normalised to
    the fully-qualified form."""
    if "." in table:
        return table
    return f"raw.{table}"


def finding_risk(finding: dict) -> float:
    """Risk contributed by a single finding: how much this class of failure
    matters, scaled by how severe this instance is, scaled by how important
    the table is."""
    weight = CHECK_RISK_WEIGHTS.get(finding.get("check_name", ""), DEFAULT_CHECK_RISK_WEIGHT)
    severity = SEVERITY_MULTIPLIERS.get(finding.get("severity", "WARNING"), 0.4)
    criticality = TABLE_CRITICALITY.get(
        canonical_table(finding.get("table", "")), DEFAULT_TABLE_CRITICALITY
    )
    return weight * severity * criticality


def band_for(score: float) -> str:
    for threshold, label in HEALTH_BANDS:
        if score >= threshold:
            return label
    return HEALTH_BANDS[-1][1]


@dataclass
class HealthScore:
    score: float
    band: str
    total_risk: float
    by_table: dict[str, float] = field(default_factory=dict)
    by_severity: dict[str, int] = field(default_factory=dict)
    top_contributors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "band": self.band,
            "total_risk": round(self.total_risk, 2),
            "by_table": {t: round(v, 2) for t, v in self.by_table.items()},
            "by_severity": self.by_severity,
            "top_contributors": self.top_contributors,
        }


def score_findings(findings: list[dict], top_n: int = 3) -> HealthScore:
    """Aggregate findings into an overall health score.

    Starts from a perfect 100 and subtracts each finding's risk, floored at
    0. Linear on purpose: doubling the number of critical findings should
    visibly double the damage, and anyone reading the report should be able
    to recompute the number by hand from the findings table.
    """
    by_table: dict[str, float] = {}
    by_severity: dict[str, int] = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    scored: list[tuple[float, dict]] = []

    total_risk = 0.0
    for finding in findings:
        risk = finding_risk(finding)
        total_risk += risk
        table = canonical_table(finding.get("table", "unknown"))
        by_table[table] = by_table.get(table, 0.0) + risk
        severity = finding.get("severity", "WARNING")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        scored.append((risk, finding))

    score = max(0.0, 100.0 - total_risk)
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_contributors = [
        {
            "check_name": f.get("check_name"),
            "table": f.get("table"),
            "column": f.get("column"),
            "severity": f.get("severity"),
            "risk": round(risk, 2),
        }
        for risk, f in scored[:top_n]
    ]

    return HealthScore(
        score=round(score, 1),
        band=band_for(score),
        total_risk=total_risk,
        by_table=by_table,
        by_severity=by_severity,
        top_contributors=top_contributors,
    )


def table_scores(findings: list[dict]) -> dict[str, float]:
    """Per-table health scores, on the same 0-100 scale as the overall one.
    A single unhealthy table shouldn't be hidden by three healthy ones."""
    per_table: dict[str, float] = {}
    for finding in findings:
        table = canonical_table(finding.get("table", "unknown"))
        per_table[table] = per_table.get(table, 0.0) + finding_risk(finding)
    return {table: round(max(0.0, 100.0 - risk), 1) for table, risk in per_table.items()}

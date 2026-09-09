"""Cross-system reconciliation.

Every check in agent/checks.py looks at one table in isolation. That is
exactly the class of test that cannot catch the most expensive kind of
enterprise data problem: two systems that are each internally consistent
and disagree with each other. The CRM says twelve deals closed for
$400k; the ERP invoiced $340k. Both tables pass every not-null, uniqueness
and referential-integrity check they have. The gap between them is the
finding.

Rules are declared in agent/config.py so adding a reconciliation is a
config entry, not new code.
"""
from __future__ import annotations

from typing import Any

import duckdb

from agent.models import Finding, Severity


def _rule_sql(rule: dict[str, Any]) -> str:
    group_by = rule["group_by"]
    left_filter = rule.get("left_filter") or "true"
    right_filter = rule.get("right_filter") or "true"
    return f"""
        with left_side as (
            select {group_by} as key, sum({rule['left_amount']}) as amount
            from {rule['left_table']}
            where {left_filter}
            group by 1
        ),
        right_side as (
            select {group_by} as key, sum({rule['right_amount']}) as amount
            from {rule['right_table']}
            where {right_filter}
            group by 1
        )
        select
            coalesce(l.key, r.key) as key,
            coalesce(l.amount, 0) as left_amount,
            coalesce(r.amount, 0) as right_amount
        from left_side l
        full outer join right_side r on l.key = r.key
    """


def reconcile(con: duckdb.DuckDBPyConnection, rule: dict[str, Any]) -> list[Finding]:
    """Run one reconciliation rule and return the findings it produces.

    Two distinct failure modes get two distinct findings, because they have
    different root causes and different fixes:

    * coverage -- one system has the entity and the other has never heard of
      it (a feed that never ran, a sync that silently dropped records).
    * totals -- both systems know about it and disagree on the number (a
      partial load, a currency or rounding bug, an amount edited in one
      system only).
    """
    try:
        rows = con.execute(_rule_sql(rule)).fetchall()
    except duckdb.Error as exc:
        # One of the two systems is no longer queryable (a dropped table
        # upstream). Losing the ability to reconcile is itself the finding.
        return [
            Finding(
                check_name="reconciliation_unavailable",
                severity=Severity.CRITICAL,
                table=rule["left_table"],
                message=(
                    f"{rule['name']}: reconciliation could not run because a "
                    f"source is unavailable ({exc.__class__.__name__}). The two "
                    "systems are now unverified against each other."
                ),
                details={"rule": rule["name"], "error": str(exc)[:200]},
            )
        ]
    tolerance = float(rule.get("tolerance_pct", 0.01))
    name = rule["name"]

    missing_right: list[Any] = []
    missing_left: list[Any] = []
    mismatched: list[dict] = []
    total_left = 0.0
    total_right = 0.0

    for key, left_amount, right_amount in rows:
        left_amount = float(left_amount or 0.0)
        right_amount = float(right_amount or 0.0)
        total_left += left_amount
        total_right += right_amount

        if left_amount != 0.0 and right_amount == 0.0:
            missing_right.append(key)
            continue
        if right_amount != 0.0 and left_amount == 0.0:
            missing_left.append(key)
            continue

        scale = max(abs(left_amount), abs(right_amount))
        if scale == 0.0:
            continue
        variance = abs(left_amount - right_amount) / scale
        if variance > tolerance:
            mismatched.append(
                {
                    "key": key,
                    "left": round(left_amount, 2),
                    "right": round(right_amount, 2),
                    "variance": round(variance, 4),
                }
            )

    findings: list[Finding] = []

    if missing_right or missing_left:
        severity = Severity.CRITICAL if missing_right else Severity.WARNING
        findings.append(
            Finding(
                check_name="reconciliation_coverage",
                severity=severity,
                table=rule["left_table"],
                message=(
                    f"{name}: {len(missing_right)} key(s) present in "
                    f"{rule['left_table']} with no matching rows in "
                    f"{rule['right_table']}, and {len(missing_left)} the other way round."
                ),
                details={
                    "rule": name,
                    "missing_in_right": missing_right[:10],
                    "missing_in_left": missing_left[:10],
                    "missing_in_right_count": len(missing_right),
                    "missing_in_left_count": len(missing_left),
                },
            )
        )

    if mismatched:
        aggregate_scale = max(abs(total_left), abs(total_right)) or 1.0
        aggregate_variance = abs(total_left - total_right) / aggregate_scale
        severity = (
            Severity.CRITICAL if aggregate_variance > tolerance else Severity.WARNING
        )
        mismatched.sort(key=lambda m: m["variance"], reverse=True)
        findings.append(
            Finding(
                check_name="reconciliation_totals",
                severity=severity,
                table=rule["left_table"],
                message=(
                    f"{name}: {len(mismatched)} key(s) disagree between "
                    f"{rule['left_table']} and {rule['right_table']}. Aggregate "
                    f"variance {aggregate_variance:.2%} "
                    f"({total_left:,.2f} vs {total_right:,.2f})."
                ),
                details={
                    "rule": name,
                    "mismatched_count": len(mismatched),
                    "aggregate_left": round(total_left, 2),
                    "aggregate_right": round(total_right, 2),
                    "aggregate_variance": round(aggregate_variance, 4),
                    "worst_offenders": mismatched[:5],
                },
            )
        )

    return findings


def run_reconciliations(
    con: duckdb.DuckDBPyConnection, rules: list[dict[str, Any]]
) -> list[Finding]:
    findings: list[Finding] = []
    for rule in rules:
        findings.extend(reconcile(con, rule))
    return findings

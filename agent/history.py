"""Run history: what turns a point-in-time check into a trend.

A single run can only say "here is the state right now". Persisting every
run lets the Guardian answer the more useful questions: is quality
degrading, is this row count unusual *for this table*, and has this exact
finding been open for six runs. History lives in the same warehouse as the
data it describes, so it is queryable with plain SQL like anything else.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

import duckdb

from agent.config import HEALTH_DEGRADATION_THRESHOLD
from agent.models import Finding, Severity

# Not "guardian": the database file is guardian.duckdb, so DuckDB already
# has a *catalog* by that name, and "guardian.run_history" then resolves
# ambiguously ("use a fully qualified path like guardian.guardian"). Naming
# the schema for what it holds avoids the collision entirely.
HISTORY_SCHEMA = "guardian_history"
RUNS_TABLE = f"{HISTORY_SCHEMA}.run_history"
FINDINGS_TABLE = f"{HISTORY_SCHEMA}.run_findings"
ROW_COUNTS_TABLE = f"{HISTORY_SCHEMA}.run_row_counts"


def ensure_history_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Idempotently create the history tables. Safe to call on every run."""
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {HISTORY_SCHEMA}")
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {RUNS_TABLE} (
            run_id VARCHAR PRIMARY KEY,
            run_at TIMESTAMP,
            health_score DOUBLE,
            band VARCHAR,
            total_findings INTEGER,
            critical_count INTEGER,
            warning_count INTEGER,
            info_count INTEGER
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {FINDINGS_TABLE} (
            run_id VARCHAR,
            check_name VARCHAR,
            severity VARCHAR,
            table_name VARCHAR,
            column_name VARCHAR,
            message VARCHAR,
            risk DOUBLE,
            details VARCHAR
        )
    """)
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {ROW_COUNTS_TABLE} (
            run_id VARCHAR,
            run_at TIMESTAMP,
            table_name VARCHAR,
            row_count BIGINT
        )
    """)


def new_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%fZ")


@dataclass
class RunRecord:
    run_id: str
    run_at: datetime
    health_score: float
    band: str
    total_findings: int
    critical_count: int


def record_run(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    health: dict,
    findings: list[dict],
    row_counts: dict[str, int] | None = None,
    now: datetime | None = None,
) -> None:
    """Persist one run: its score, every finding, and the row count of every
    monitored table (which is what later runs compute their z-scores from)."""
    ensure_history_schema(con)
    run_at = now or datetime.now(timezone.utc)
    by_severity = health.get("by_severity", {})

    con.execute(
        f"INSERT INTO {RUNS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            run_id,
            run_at,
            health.get("score", 100.0),
            health.get("band", "HEALTHY"),
            len(findings),
            by_severity.get("CRITICAL", 0),
            by_severity.get("WARNING", 0),
            by_severity.get("INFO", 0),
        ],
    )

    for finding in findings:
        con.execute(
            f"INSERT INTO {FINDINGS_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                finding.get("check_name"),
                finding.get("severity"),
                finding.get("table"),
                finding.get("column"),
                finding.get("message"),
                float(finding.get("details", {}).get("risk", 0.0) or 0.0),
                json.dumps(finding.get("details", {}), default=str),
            ],
        )

    for table, count in (row_counts or {}).items():
        con.execute(
            f"INSERT INTO {ROW_COUNTS_TABLE} VALUES (?, ?, ?, ?)",
            [run_id, run_at, table, count],
        )


def recent_runs(con: duckdb.DuckDBPyConnection, limit: int = 10) -> list[RunRecord]:
    ensure_history_schema(con)
    rows = con.execute(
        f"""
        select run_id, run_at, health_score, band, total_findings, critical_count
        from {RUNS_TABLE}
        order by run_at desc
        limit {int(limit)}
        """
    ).fetchall()
    return [RunRecord(*row) for row in rows]


def historical_row_counts(
    con: duckdb.DuckDBPyConnection, table: str, exclude_run_id: str | None = None
) -> list[int]:
    """Every previously recorded row count for a table, oldest first."""
    ensure_history_schema(con)
    sql = f"select row_count from {ROW_COUNTS_TABLE} where table_name = ?"
    params: list = [table]
    if exclude_run_id:
        sql += " and run_id <> ?"
        params.append(exclude_run_id)
    sql += " order by run_at asc"
    return [row[0] for row in con.execute(sql, params).fetchall()]


def check_health_degradation(
    current_score: float,
    previous: RunRecord | None,
    threshold: float = HEALTH_DEGRADATION_THRESHOLD,
) -> Finding | None:
    """Flag a sharp drop versus the previous run.

    This exists because an absolute score hides direction: 78 is a fine
    number on its own and an alarming one if yesterday was 97. Trend is a
    finding in its own right, not a footnote on the report.
    """
    if previous is None:
        return None
    drop = previous.health_score - current_score
    if drop < threshold:
        return None
    severity = Severity.CRITICAL if drop >= threshold * 2 else Severity.WARNING
    return Finding(
        check_name="health_degradation",
        severity=severity,
        table="__run__",
        message=(
            f"Data-quality health dropped {drop:.1f} points since the previous run "
            f"({previous.health_score:.1f} -> {current_score:.1f})."
        ),
        details={
            "previous_score": previous.health_score,
            "current_score": current_score,
            "drop": round(drop, 1),
            "previous_run_id": previous.run_id,
        },
    )


def summarize_trend(runs: list[RunRecord]) -> dict:
    """A compact trend summary for the report: where the score has been, and
    whether the current run is better or worse than the recent norm."""
    if not runs:
        return {"runs": 0}
    scores = [r.health_score for r in runs]
    current = scores[0]
    prior = scores[1:]
    summary = {
        "runs": len(runs),
        "current_score": current,
        "best_score": max(scores),
        "worst_score": min(scores),
    }
    if prior:
        mean_prior = statistics.fmean(prior)
        summary["mean_prior_score"] = round(mean_prior, 1)
        summary["delta_vs_mean"] = round(current - mean_prior, 1)
    return summary

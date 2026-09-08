"""Deterministic, testable data-quality and schema-drift checks.

This module contains no LLM calls on purpose: every check here is a plain
SQL query with a clear pass/fail rule, so it can be unit tested and trusted
on its own. agent/guardian_agent.py is the layer above this one -- it hands
these results to Claude to reason about, prioritize, and explain, but it
never has to "trust" a model to *detect* the problem in the first place.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

import duckdb

from agent.models import Finding, Severity
from ingestion.db import fetch_row, fetch_scalar

# ---------------------------------------------------------------------------
# Schema snapshot + drift detection
# ---------------------------------------------------------------------------

def snapshot_schema(con: duckdb.DuckDBPyConnection, schema: str = "raw") -> dict:
    """Return {table_name: {column_name: column_type}} for every table in
    the given schema."""
    rows = con.execute(
        """
        select table_name, column_name, data_type
        from information_schema.columns
        where table_schema = ?
        order by table_name, ordinal_position
        """,
        [schema],
    ).fetchall()

    snapshot: dict[str, dict[str, str]] = {}
    for table_name, column_name, data_type in rows:
        snapshot.setdefault(table_name, {})[column_name] = data_type
    return snapshot


def save_baseline(snapshot: dict, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(snapshot, indent=2, sort_keys=True))


def load_baseline(path: str) -> dict:
    if not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


def diff_schema(baseline: dict, current: dict) -> list[Finding]:
    """Compare a stored baseline schema snapshot against the current one and
    return a Finding per detected change. This is what catches an upstream
    system silently renaming, dropping, or retyping a column."""
    findings: list[Finding] = []

    baseline_tables = set(baseline.keys())
    current_tables = set(current.keys())

    for table in sorted(current_tables - baseline_tables):
        findings.append(
            Finding(
                check_name="schema_new_table",
                severity=Severity.INFO,
                table=table,
                message=f"New table '{table}' appeared that wasn't in the baseline.",
            )
        )

    for table in sorted(baseline_tables - current_tables):
        findings.append(
            Finding(
                check_name="schema_dropped_table",
                severity=Severity.CRITICAL,
                table=table,
                message=f"Table '{table}' existed in the baseline but is missing now.",
            )
        )

    for table in sorted(baseline_tables & current_tables):
        base_cols = baseline[table]
        cur_cols = current[table]

        for col in sorted(set(cur_cols) - set(base_cols)):
            findings.append(
                Finding(
                    check_name="schema_new_column",
                    severity=Severity.INFO,
                    table=table,
                    column=col,
                    message=f"New column '{col}' ({cur_cols[col]}) appeared on '{table}'.",
                )
            )

        for col in sorted(set(base_cols) - set(cur_cols)):
            findings.append(
                Finding(
                    check_name="schema_dropped_column",
                    severity=Severity.CRITICAL,
                    table=table,
                    column=col,
                    message=(
                        f"Column '{col}' ({base_cols[col]}) existed on '{table}' in the "
                        "baseline but is gone now -- likely an upstream schema change "
                        "or rename."
                    ),
                )
            )

        for col in sorted(set(base_cols) & set(cur_cols)):
            if base_cols[col] != cur_cols[col]:
                findings.append(
                    Finding(
                        check_name="schema_type_changed",
                        severity=Severity.CRITICAL,
                        table=table,
                        column=col,
                        message=(
                            f"Column '{col}' on '{table}' changed type from "
                            f"{base_cols[col]} to {cur_cols[col]}."
                        ),
                        details={"from_type": base_cols[col], "to_type": cur_cols[col]},
                    )
                )

    return findings


# ---------------------------------------------------------------------------
# Row-level data-quality checks
# ---------------------------------------------------------------------------

def check_not_null(con: duckdb.DuckDBPyConnection, table: str, column: str) -> Finding | None:
    total, nulls = fetch_row(
        con, f"select count(*), count(*) filter (where {column} is null) from {table}"
    )
    if total == 0 or nulls == 0:
        return None
    rate = nulls / total
    severity = Severity.CRITICAL if rate > 0.01 else Severity.WARNING
    return Finding(
        check_name="not_null",
        severity=severity,
        table=table,
        column=column,
        message=f"{nulls}/{total} rows ({rate:.1%}) have a null '{column}'.",
        details={"total": total, "nulls": nulls, "rate": rate},
    )


def check_unique(con: duckdb.DuckDBPyConnection, table: str, column: str) -> Finding | None:
    total, distinct = fetch_row(
        con, f"select count(*), count(distinct {column}) from {table}"
    )
    if total == distinct:
        return None
    dupes = total - distinct
    return Finding(
        check_name="uniqueness",
        severity=Severity.CRITICAL,
        table=table,
        column=column,
        message=f"'{column}' on {table} is not unique: {dupes} duplicate value(s) found.",
        details={"total": total, "distinct": distinct, "duplicates": dupes},
    )


def check_referential_integrity(
    con: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    ref_table: str,
    ref_column: str,
) -> Finding | None:
    orphans = fetch_scalar(
        con,
        f"""
        select count(*) from {table} t
        where t.{column} is not null
          and not exists (
              select 1 from {ref_table} r where r.{ref_column} = t.{column}
          )
        """,
    )
    if orphans == 0:
        return None
    return Finding(
        check_name="referential_integrity",
        severity=Severity.CRITICAL,
        table=table,
        column=column,
        message=(
            f"{orphans} row(s) in {table} reference a {column} that doesn't "
            f"exist in {ref_table}.{ref_column}."
        ),
        details={"orphans": orphans, "ref_table": ref_table, "ref_column": ref_column},
    )


def check_value_range(
    con: duckdb.DuckDBPyConnection,
    table: str,
    column: str,
    min_value: float | None = None,
    max_value: float | None = None,
) -> Finding | None:
    clauses = []
    if min_value is not None:
        clauses.append(f"{column} < {min_value}")
    if max_value is not None:
        clauses.append(f"{column} > {max_value}")
    if not clauses:
        return None

    where = " or ".join(clauses)
    violations = fetch_scalar(
        con, f"select count(*) from {table} where {column} is not null and ({where})"
    )
    if violations == 0:
        return None
    return Finding(
        check_name="value_range",
        severity=Severity.WARNING,
        table=table,
        column=column,
        message=f"{violations} row(s) in {table}.{column} fall outside the expected range.",
        details={"violations": violations, "min": min_value, "max": max_value},
    )


def check_freshness(
    con: duckdb.DuckDBPyConnection,
    table: str,
    timestamp_column: str,
    max_age_hours: float,
) -> Finding | None:
    age_hours = fetch_scalar(
        con,
        f"""
        select date_diff('hour', max({timestamp_column}), current_timestamp)
        from {table}
        """,
    )
    if age_hours is None or age_hours <= max_age_hours:
        return None
    return Finding(
        check_name="freshness",
        severity=Severity.WARNING,
        table=table,
        column=timestamp_column,
        message=(
            f"Most recent {timestamp_column} in {table} is {age_hours:.0f}h old, "
            f"exceeding the {max_age_hours:.0f}h freshness SLA -- the connector "
            "may have stalled."
        ),
        details={"age_hours": age_hours, "sla_hours": max_age_hours},
    )


def check_row_count_anomaly(
    con: duckdb.DuckDBPyConnection,
    table: str,
    baseline_count: int,
    tolerance_pct: float = 0.5,
) -> Finding | None:
    """Flag a table whose row count deviates from a known-good baseline by
    more than `tolerance_pct` (0.5 = 50%). Catches both silent ingestion
    failures (count collapses) and runaway duplication (count explodes)."""
    if baseline_count == 0:
        return None
    current = fetch_scalar(con, f"select count(*) from {table}")
    deviation = abs(current - baseline_count) / baseline_count
    if deviation <= tolerance_pct:
        return None
    return Finding(
        check_name="row_count_anomaly",
        severity=Severity.WARNING,
        table=table,
        message=(
            f"{table} has {current} rows, a {deviation:.0%} deviation from the "
            f"baseline of {baseline_count}."
        ),
        details={"current": current, "baseline": baseline_count, "deviation": deviation},
    )


def check_row_count_zscore(
    con: duckdb.DuckDBPyConnection,
    table: str,
    history: list[int],
    z_threshold: float = 3.0,
    min_history: int = 3,
) -> Finding | None:
    """Volume anomaly detection against a table's own history, rather than a
    fixed percentage tolerance.

    check_row_count_anomaly asks "did this move more than 50% from a single
    baseline". That threshold is wrong for almost every table: a feed that
    naturally swings 40% run to run cries wolf, and a rock-steady one can
    drift 45% without a peep. This asks the better question -- "is today's
    count unusual *for this table*" -- by comparing it to the mean and
    spread of its own recorded history.

    Returns None until there are at least `min_history` prior runs, because
    a standard deviation computed from two points is not a signal.
    """
    if len(history) < min_history:
        return None

    current = fetch_scalar(con, f"select count(*) from {table}")
    mean = statistics.fmean(history)
    stdev = statistics.pstdev(history)

    if stdev == 0:
        # A table whose count has never moved is a contract of sorts. Any
        # change at all is worth surfacing, but as a warning rather than a
        # critical -- one prior period of stability is thin evidence.
        if current == mean:
            return None
        return Finding(
            check_name="row_count_zscore",
            severity=Severity.WARNING,
            table=table,
            message=(
                f"{table} has {current} rows after {len(history)} run(s) of a "
                f"perfectly stable {int(mean)}. No historical variance to "
                "compare against, so any movement is notable."
            ),
            details={
                "current": current,
                "mean": mean,
                "stdev": 0.0,
                "z_score": None,
                "history_points": len(history),
            },
        )

    z_score = (current - mean) / stdev
    if abs(z_score) < z_threshold:
        return None

    severity = Severity.CRITICAL if abs(z_score) >= z_threshold * 2 else Severity.WARNING
    direction = "above" if z_score > 0 else "below"
    return Finding(
        check_name="row_count_zscore",
        severity=severity,
        table=table,
        message=(
            f"{table} has {current} rows, {abs(z_score):.1f} standard deviations "
            f"{direction} its historical mean of {mean:,.0f} "
            f"(sigma {stdev:,.0f}, {len(history)} prior runs)."
        ),
        details={
            "current": current,
            "mean": round(mean, 2),
            "stdev": round(stdev, 2),
            "z_score": round(z_score, 2),
            "history_points": len(history),
        },
    )


# ---------------------------------------------------------------------------
# PII detection (column-name heuristics -- deliberately simple and auditable)
# ---------------------------------------------------------------------------

PII_COLUMN_PATTERNS = {
    "email": re.compile(r"e[-_]?mail", re.IGNORECASE),
    "name": re.compile(r"(full[-_]?name|first[-_]?name|last[-_]?name|^name$)", re.IGNORECASE),
    "phone": re.compile(r"phone", re.IGNORECASE),
    "address": re.compile(r"address", re.IGNORECASE),
    "dob": re.compile(r"(date[-_]?of[-_]?birth|^dob$)", re.IGNORECASE),
    "ssn_or_gov_id": re.compile(r"(ssn|social[-_]?security|national[-_]?id|passport)", re.IGNORECASE),
}


def detect_pii_columns(schema: dict) -> list[Finding]:
    """Flag columns whose *name* suggests they hold PII, so they can be
    checked against the masking policies defined in terraform/. This is a
    governance signal, not a guarantee -- a real implementation would also
    sample values, since a column called 'notes' can hold PII too."""
    findings = []
    for table, columns in schema.items():
        for column in columns:
            for pii_type, pattern in PII_COLUMN_PATTERNS.items():
                if pattern.search(column):
                    findings.append(
                        Finding(
                            check_name="pii_column_detected",
                            severity=Severity.WARNING,
                            table=table,
                            column=column,
                            message=(
                                f"Column '{column}' on '{table}' looks like it holds "
                                f"{pii_type} data. Confirm it's masked or access-controlled."
                            ),
                            details={"pii_type": pii_type},
                        )
                    )
                    break
    return findings

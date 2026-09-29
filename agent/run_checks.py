"""Single entry point that runs every deterministic check and returns a flat
list of Findings. Used by both the CLI (`python -m agent.run_checks`) and
the LangGraph agent's collect step.

Three tiers of check run here, and the ordering matters:

1. Structural  -- schema drift and PII, computed from an information_schema
   snapshot before any row is queried.
2. Row-level   -- uniqueness, not-null, referential integrity, ranges,
   freshness, volume; one table at a time.
3. Cross-system -- reconciliation between tables that should agree with each
   other; the only tier that can fail while every table individually passes.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from agent.config import (
    RECONCILIATION_RULES,
    ROW_COUNT_BASELINE_PATH,
    SCHEMA_BASELINE_PATH,
    TABLE_CHECKS,
)
from agent.checks import (
    check_freshness,
    check_not_null,
    check_referential_integrity,
    check_row_count_anomaly,
    check_row_count_zscore,
    check_unique,
    check_value_range,
    detect_pii_columns,
    diff_schema,
    load_baseline,
    save_baseline,
    snapshot_schema,
)
from agent.models import Finding, Severity
from agent.reconciliation import run_reconciliations
from ingestion.db import fetch_scalar


def _load_row_count_baseline(path: str) -> dict:
    if not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


def current_row_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row count of every monitored table. Recorded into run history so
    later runs have a distribution to compute z-scores against."""
    counts: dict[str, int] = {}
    for table in TABLE_CHECKS:
        try:
            counts[table] = fetch_scalar(con, f"select count(*) from {table}")
        except duckdb.Error:
            # A table dropped upstream is already reported by schema diff;
            # its absence shouldn't take down history recording too.
            continue
    return counts


def _save_row_count_baseline(con: duckdb.DuckDBPyConnection, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(current_row_counts(con), indent=2, sort_keys=True))


def snapshot_baselines(con: duckdb.DuckDBPyConnection) -> None:
    """Capture the current schema + row counts as the new 'known good'
    baseline. Run this once against clean data, then commit the resulting
    JSON files -- that's what future runs get diffed against."""
    save_baseline(snapshot_schema(con), SCHEMA_BASELINE_PATH)
    _save_row_count_baseline(con, ROW_COUNT_BASELINE_PATH)


def run_all_checks(
    con: duckdb.DuckDBPyConnection,
    history_counts: dict[str, list[int]] | None = None,
) -> list[Finding]:
    """Run every check. `history_counts` maps a table to its previously
    recorded row counts; when supplied, the statistical volume check runs
    alongside the fixed-tolerance one."""
    findings: list[Finding] = []
    history_counts = history_counts or {}

    # --- Tier 1: structural -------------------------------------------------
    current_schema = snapshot_schema(con)
    baseline_schema = load_baseline(SCHEMA_BASELINE_PATH)
    if baseline_schema:
        findings.extend(diff_schema(baseline_schema, current_schema))
    findings.extend(detect_pii_columns(current_schema))

    row_count_baseline = _load_row_count_baseline(ROW_COUNT_BASELINE_PATH)

    # --- Tier 2: row-level --------------------------------------------------
    for table, rules in TABLE_CHECKS.items():
        # Skip row-level checks entirely for a table that schema-diff already
        # flagged as dropped -- querying it would just raise a SQL error.
        table_name = table.split(".", 1)[1]
        if table_name not in current_schema:
            continue

        # Every column a rule names can disappear upstream, not just the
        # table. Querying a missing column raises a BinderException and takes
        # down the whole run, so each check below is skipped for columns
        # schema diff has already reported as dropped.
        columns = current_schema[table_name]

        pk = rules.get("primary_key")
        if pk and pk in columns:
            finding = check_unique(con, table, pk)
            if finding:
                findings.append(finding)

        for column in rules.get("not_null", []):
            if column not in columns:
                continue  # already reported by schema diff
            finding = check_not_null(con, table, column)
            if finding:
                findings.append(finding)

        for fk in rules.get("foreign_keys", []):
            if fk["column"] not in columns:
                continue  # already reported by schema diff
            # If the referenced table (or its key column) has itself been
            # dropped upstream, the integrity query would raise and take down
            # the whole run -- at exactly the moment the run matters most.
            # Report the loss of verifiability as its own finding and carry on.
            ref_name = fk["ref_table"].split(".", 1)[-1]
            if ref_name not in current_schema or fk["ref_column"] not in current_schema[ref_name]:
                missing = (
                    f"table {fk['ref_table']}" if ref_name not in current_schema
                    else f"column {fk['ref_table']}.{fk['ref_column']}"
                )
                findings.append(
                    Finding(
                        check_name="foreign_key_unverifiable",
                        severity=Severity.CRITICAL,
                        table=table,
                        column=fk["column"],
                        message=(
                            f"Cannot verify '{fk['column']}' on {table}: reference "
                            f"{missing} no longer exists, so every "
                            "row's key is now unvalidated."
                        ),
                        details={"ref_table": fk["ref_table"], "ref_column": fk["ref_column"]},
                    )
                )
                continue
            finding = check_referential_integrity(
                con, table, fk["column"], fk["ref_table"], fk["ref_column"]
            )
            if finding:
                findings.append(finding)

        for column, bounds in rules.get("ranges", {}).items():
            if column not in columns:
                continue  # already reported by schema diff
            finding = check_value_range(
                con, table, column, bounds.get("min"), bounds.get("max")
            )
            if finding:
                findings.append(finding)

        freshness = rules.get("freshness")
        if freshness and freshness["column"] in columns:
            finding = check_freshness(
                con, table, freshness["column"], freshness["max_age_hours"]
            )
            if finding:
                findings.append(finding)

        if table in row_count_baseline:
            finding = check_row_count_anomaly(con, table, row_count_baseline[table])
            if finding:
                findings.append(finding)

        table_history = history_counts.get(table, [])
        if table_history:
            finding = check_row_count_zscore(con, table, table_history)
            if finding:
                findings.append(finding)

    # --- Tier 3: cross-system ----------------------------------------------
    findings.extend(run_reconciliations(con, RECONCILIATION_RULES))

    return findings


def main() -> None:
    import argparse

    from agent.history import historical_row_counts
    from agent.scoring import score_findings
    from ingestion.db import connect

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot-baseline",
        action="store_true",
        help="Capture current schema + row counts as the new baseline instead of checking.",
    )
    args = parser.parse_args()

    con = connect(read_only=False)

    if args.snapshot_baseline:
        snapshot_baselines(con)
        print(f"Baseline written to {SCHEMA_BASELINE_PATH} and {ROW_COUNT_BASELINE_PATH}")
        con.close()
        return

    history = {table: historical_row_counts(con, table) for table in TABLE_CHECKS}
    findings = run_all_checks(con, history_counts=history)
    health = score_findings([f.to_dict() for f in findings])
    con.close()

    if not findings:
        print("No issues found. All checks passed. Health score: 100.0 (HEALTHY)")
        return

    by_severity = health.by_severity
    print(f"Health score: {health.score} ({health.band})")
    print(f"{len(findings)} finding(s): "
          f"{by_severity['CRITICAL']} critical, "
          f"{by_severity['WARNING']} warning, "
          f"{by_severity['INFO']} info\n")
    for f in sorted(findings, key=lambda x: x.severity.value):
        col = f".{f.column}" if f.column else ""
        print(f"[{f.severity.value:8s}] {f.table}{col}: {f.message}")


if __name__ == "__main__":
    main()

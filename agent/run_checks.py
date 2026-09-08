"""Single entry point that runs every deterministic check and returns a
flat list of Findings. Used by both the CLI (`python -m agent.run_checks`)
and the LangGraph agent as a tool.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from agent.config import ROW_COUNT_BASELINE_PATH, SCHEMA_BASELINE_PATH, TABLE_CHECKS
from agent.checks import (
    check_freshness,
    check_not_null,
    check_referential_integrity,
    check_row_count_anomaly,
    check_unique,
    check_value_range,
    detect_pii_columns,
    diff_schema,
    load_baseline,
    save_baseline,
    snapshot_schema,
)
from agent.models import Finding


def _load_row_count_baseline(path: str) -> dict:
    if not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


def _save_row_count_baseline(con: duckdb.DuckDBPyConnection, path: str) -> None:
    counts = {
        table: con.execute(f"select count(*) from {table}").fetchone()[0]
        for table in TABLE_CHECKS
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(counts, indent=2, sort_keys=True))


def snapshot_baselines(con: duckdb.DuckDBPyConnection) -> None:
    """Capture the current schema + row counts as the new 'known good'
    baseline. Run this once against clean data, then commit the resulting
    JSON files -- that's what future runs get diffed against."""
    save_baseline(snapshot_schema(con), SCHEMA_BASELINE_PATH)
    _save_row_count_baseline(con, ROW_COUNT_BASELINE_PATH)


def run_all_checks(con: duckdb.DuckDBPyConnection) -> list[Finding]:
    findings: list[Finding] = []

    current_schema = snapshot_schema(con)
    baseline_schema = load_baseline(SCHEMA_BASELINE_PATH)
    if baseline_schema:
        findings.extend(diff_schema(baseline_schema, current_schema))
    findings.extend(detect_pii_columns(current_schema))

    row_count_baseline = _load_row_count_baseline(ROW_COUNT_BASELINE_PATH)

    for table, rules in TABLE_CHECKS.items():
        # Skip row-level checks entirely for a table that schema-diff already
        # flagged as dropped -- querying it would just raise a SQL error.
        table_name = table.split(".", 1)[1]
        if table_name not in current_schema:
            continue

        pk = rules.get("primary_key")
        if pk and pk in current_schema[table_name]:
            finding = check_unique(con, table, pk)
            if finding:
                findings.append(finding)

        for column in rules.get("not_null", []):
            if column not in current_schema[table_name]:
                continue  # already reported by schema diff
            finding = check_not_null(con, table, column)
            if finding:
                findings.append(finding)

        for fk in rules.get("foreign_keys", []):
            finding = check_referential_integrity(
                con, table, fk["column"], fk["ref_table"], fk["ref_column"]
            )
            if finding:
                findings.append(finding)

        for column, bounds in rules.get("ranges", {}).items():
            finding = check_value_range(
                con, table, column, bounds.get("min"), bounds.get("max")
            )
            if finding:
                findings.append(finding)

        freshness = rules.get("freshness")
        if freshness:
            finding = check_freshness(
                con, table, freshness["column"], freshness["max_age_hours"]
            )
            if finding:
                findings.append(finding)

        if table in row_count_baseline:
            finding = check_row_count_anomaly(con, table, row_count_baseline[table])
            if finding:
                findings.append(finding)

    return findings


def main() -> None:
    import argparse

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

    findings = run_all_checks(con)
    con.close()

    if not findings:
        print("No issues found. All checks passed.")
        return

    by_severity = {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
    for f in findings:
        by_severity[f.severity.value] += 1

    print(f"{len(findings)} finding(s): "
          f"{by_severity['CRITICAL']} critical, "
          f"{by_severity['WARNING']} warning, "
          f"{by_severity['INFO']} info\n")
    for f in sorted(findings, key=lambda x: x.severity.value):
        col = f".{f.column}" if f.column else ""
        print(f"[{f.severity.value:8s}] {f.table}{col}: {f.message}")


if __name__ == "__main__":
    main()

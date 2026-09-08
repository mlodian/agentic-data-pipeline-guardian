"""Quarantine: the one action this agent can take on the data itself.

Reporting a problem is cheap. Acting on data is not, so this module is
built around a single principle -- the agent may isolate rows it can prove
are bad, and it may never do so silently or irreversibly.

That shows up as three constraints:

1. Only findings whose rule is declared in config.QUARANTINE_RULES are
   eligible. The agent cannot invent a reason to delete rows.
2. Nothing happens at all unless GUARDIAN_ENABLE_QUARANTINE=true. The
   default is a dry run that reports exactly what *would* move.
3. Rows are copied into the quarantine schema with their run id and reason
   before they are removed from raw, so every quarantined row is
   recoverable and auditable with a plain SELECT.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import duckdb

from agent.config import QUARANTINE_RULES, QUARANTINE_SCHEMA
from ingestion.db import fetch_scalar


def quarantine_enabled() -> bool:
    return os.environ.get("GUARDIAN_ENABLE_QUARANTINE", "false").lower() == "true"


@dataclass
class QuarantineAction:
    """One planned or executed quarantine, kept as data so the plan can be
    shown to a human (or written into the report) before anything runs."""

    table: str
    column: str
    check_name: str
    predicate: str
    reason: str
    matched_rows: int = 0
    applied: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "table": self.table,
            "column": self.column,
            "check_name": self.check_name,
            "predicate": self.predicate,
            "reason": self.reason,
            "matched_rows": self.matched_rows,
            "applied": self.applied,
            "error": self.error,
        }


@dataclass
class QuarantineResult:
    actions: list[QuarantineAction] = field(default_factory=list)
    enabled: bool = False

    @property
    def total_rows(self) -> int:
        return sum(a.matched_rows for a in self.actions)

    @property
    def applied_count(self) -> int:
        return sum(1 for a in self.actions if a.applied)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "actions": [a.to_dict() for a in self.actions],
            "total_rows": self.total_rows,
            "applied_count": self.applied_count,
        }


def plan_quarantine(findings: list[dict]) -> list[QuarantineAction]:
    """Turn findings into a quarantine plan.

    A finding only becomes an action if its check has a declared rule and it
    names a specific column -- a table-level problem like schema drift is
    never something you fix by deleting rows.
    """
    actions: list[QuarantineAction] = []
    for finding in findings:
        check_name = finding.get("check_name", "")
        rule = QUARANTINE_RULES.get(check_name)
        column = finding.get("column")
        table = finding.get("table", "")
        if not rule or not column or not table or table == "__run__":
            continue
        if "." not in table:
            table = f"raw.{table}"
        actions.append(
            QuarantineAction(
                table=table,
                column=column,
                check_name=check_name,
                predicate=rule["predicate"].format(column=column),
                reason=rule["reason"],
            )
        )
    return actions


def _quarantine_table_for(table: str) -> str:
    bare = table.split(".", 1)[-1]
    return f"{QUARANTINE_SCHEMA}.{bare}"


def execute_quarantine(
    con: duckdb.DuckDBPyConnection,
    actions: list[QuarantineAction],
    run_id: str,
    enabled: bool | None = None,
) -> QuarantineResult:
    """Count what each action matches, and -- only when explicitly enabled --
    copy those rows into the quarantine schema and remove them from raw."""
    is_enabled = quarantine_enabled() if enabled is None else enabled
    result = QuarantineResult(actions=actions, enabled=is_enabled)

    if is_enabled:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {QUARANTINE_SCHEMA}")

    for action in actions:
        try:
            action.matched_rows = fetch_scalar(
                con, f"select count(*) from {action.table} where {action.predicate}"
            )
        except duckdb.Error as exc:
            action.error = f"could not evaluate predicate: {exc}"
            continue

        if not is_enabled or action.matched_rows == 0:
            continue

        target = _quarantine_table_for(action.table)
        try:
            con.execute(f"""
                CREATE TABLE IF NOT EXISTS {target} AS
                SELECT *,
                       cast(null as varchar) as quarantine_run_id,
                       cast(null as varchar) as quarantine_reason,
                       cast(null as timestamp) as quarantined_at
                FROM {action.table} LIMIT 0
            """)
            con.execute(
                f"""
                INSERT INTO {target}
                SELECT *, ?, ?, now()
                FROM {action.table}
                WHERE {action.predicate}
                """,
                [run_id, f"{action.check_name}: {action.reason}"],
            )
            con.execute(f"DELETE FROM {action.table} WHERE {action.predicate}")
            action.applied = True
        except duckdb.Error as exc:
            # A schema change between the quarantine table's creation and now
            # is the realistic failure here. Report it as a failed action
            # rather than aborting the whole run -- the report still needs to
            # reach a human either way.
            action.error = f"quarantine failed: {exc}"

    return result

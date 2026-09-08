"""Tests for the quarantine remediation action.

This is the only place the agent touches the data, so the tests are less
about "does it work" and more about "can it do something nobody asked for".
The safety properties -- opt-in, declared rules only, copy before delete,
fully recoverable -- are each pinned by their own test.
"""
import pytest

from agent.remediation import (
    QuarantineAction,
    execute_quarantine,
    plan_quarantine,
    quarantine_enabled,
)


@pytest.fixture
def dirty(con):
    con.execute("""
        create table raw.erp_invoices as select * from (values
            (1, 50.0), (2, -30.0), (3, -10.0), (4, 75.0)
        ) t(invoice_id, amount)
    """)
    return con


def finding(check, table="raw.erp_invoices", column="amount", severity="CRITICAL"):
    return {"check_name": check, "severity": severity, "table": table,
            "column": column, "message": "", "details": {}}


def actions_for(findings):
    return plan_quarantine(findings)


# --- what the agent is allowed to plan -------------------------------------

def test_declared_rule_becomes_an_action():
    plan = actions_for([finding("value_range")])
    assert len(plan) == 1
    assert plan[0].predicate == "amount < 0"


def test_undeclared_check_is_never_quarantinable():
    """The agent cannot invent a reason to delete rows -- only checks with a
    rule in config.QUARANTINE_RULES are eligible."""
    assert actions_for([finding("uniqueness")]) == []


def test_schema_level_finding_is_never_quarantinable():
    """You do not fix a dropped column by deleting rows."""
    assert actions_for([finding("schema_dropped_column", column="is_active")]) == []


def test_finding_without_a_column_is_skipped():
    """A table-wide problem has no row predicate to build."""
    assert actions_for([finding("value_range", column=None)]) == []


def test_run_level_finding_is_skipped():
    assert actions_for([finding("value_range", table="__run__")]) == []


def test_bare_table_names_are_qualified_in_the_plan():
    assert actions_for([finding("value_range", table="erp_invoices")])[0].table == "raw.erp_invoices"


# --- dry run is the default ------------------------------------------------

def test_disabled_by_default_in_a_clean_environment(monkeypatch):
    """The safety default has to hold when nobody sets anything."""
    monkeypatch.delenv("GUARDIAN_ENABLE_QUARANTINE", raising=False)
    assert quarantine_enabled() is False


def test_dry_run_counts_rows_without_touching_them(dirty):
    result = execute_quarantine(dirty, actions_for([finding("value_range")]), "run1", enabled=False)
    assert result.total_rows == 2
    assert result.applied_count == 0
    assert dirty.execute("select count(*) from raw.erp_invoices").fetchone()[0] == 4


def test_dry_run_creates_no_quarantine_schema(dirty):
    """A dry run must leave no trace at all, not even an empty table."""
    execute_quarantine(dirty, actions_for([finding("value_range")]), "run1", enabled=False)
    schemas = [r[0] for r in dirty.execute(
        "select schema_name from information_schema.schemata").fetchall()]
    assert "quarantine" not in schemas


# --- enabled behaviour -----------------------------------------------------

def test_enabled_run_removes_the_bad_rows(dirty):
    execute_quarantine(dirty, actions_for([finding("value_range")]), "run1", enabled=True)
    assert dirty.execute("select count(*) from raw.erp_invoices where amount < 0").fetchone()[0] == 0


def test_enabled_run_leaves_the_good_rows_alone(dirty):
    """The predicate is a positive definition of a bad row; everything else
    must survive untouched."""
    execute_quarantine(dirty, actions_for([finding("value_range")]), "run1", enabled=True)
    remaining = dirty.execute("select invoice_id from raw.erp_invoices order by 1").fetchall()
    assert [r[0] for r in remaining] == [1, 4]


def test_quarantined_rows_are_recoverable(dirty):
    """Copy-before-delete is what makes this reversible; without it the agent
    is just deleting production data."""
    execute_quarantine(dirty, actions_for([finding("value_range")]), "run1", enabled=True)
    rows = dirty.execute("select invoice_id from quarantine.erp_invoices order by 1").fetchall()
    assert [r[0] for r in rows] == [2, 3]


def test_quarantined_rows_carry_run_id_and_reason(dirty):
    """Six months later, someone has to be able to answer 'why is this row
    not in the warehouse' with a single SELECT."""
    execute_quarantine(dirty, actions_for([finding("value_range")]), "run-42", enabled=True)
    run_id, reason = dirty.execute(
        "select quarantine_run_id, quarantine_reason from quarantine.erp_invoices limit 1"
    ).fetchone()
    assert run_id == "run-42"
    assert "value_range" in reason


def test_nothing_matching_means_nothing_moves(dirty):
    dirty.execute("update raw.erp_invoices set amount = abs(amount)")
    result = execute_quarantine(dirty, actions_for([finding("value_range")]), "run1", enabled=True)
    assert result.total_rows == 0
    assert result.applied_count == 0


def test_repeated_runs_accumulate_rather_than_collide(dirty):
    """The quarantine table persists across runs, so a second run must append
    to it instead of failing on an existing table."""
    execute_quarantine(dirty, actions_for([finding("value_range")]), "run1", enabled=True)
    dirty.execute("insert into raw.erp_invoices values (5, -99.0)")
    execute_quarantine(dirty, actions_for([finding("value_range")]), "run2", enabled=True)
    assert dirty.execute("select count(*) from quarantine.erp_invoices").fetchone()[0] == 3


# --- failure handling ------------------------------------------------------

def test_a_broken_predicate_is_reported_not_raised(con):
    """A column dropped upstream between check and remediation shouldn't take
    down the run -- the report still has to reach a human."""
    con.execute("create table raw.erp_invoices as select 1 as invoice_id")
    action = QuarantineAction(
        table="raw.erp_invoices", column="amount", check_name="value_range",
        predicate="amount < 0", reason="negative",
    )
    result = execute_quarantine(con, [action], "run1", enabled=True)
    assert result.actions[0].error is not None
    assert result.actions[0].applied is False

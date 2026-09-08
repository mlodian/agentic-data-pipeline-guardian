"""Tests for run history and trend detection.

History is what turns a snapshot into a trend, so these tests cover the two
things that make the trend trustworthy: runs are persisted faithfully (every
finding, every row count), and degradation is judged against the previous
run rather than an absolute threshold.
"""
from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from agent.history import (
    RunRecord,
    check_health_degradation,
    ensure_history_schema,
    historical_row_counts,
    new_run_id,
    recent_runs,
    record_run,
    summarize_trend,
)
from agent.models import Severity


@pytest.fixture
def hcon():
    con = duckdb.connect(":memory:")
    ensure_history_schema(con)
    yield con
    con.close()


def health(score, critical=0, warning=0, info=0):
    return {
        "score": score,
        "band": "HEALTHY" if score >= 90 else "DEGRADED",
        "by_severity": {"CRITICAL": critical, "WARNING": warning, "INFO": info},
    }


def a_finding(check="not_null", severity="WARNING"):
    return {
        "check_name": check, "severity": severity, "table": "raw.crm_deals",
        "column": "amount", "message": "m", "details": {"risk": 2.4},
    }


# --- schema ----------------------------------------------------------------

def test_ensure_history_schema_is_idempotent(hcon):
    """It runs on every single check, so calling it twice must be a no-op
    rather than an error."""
    ensure_history_schema(hcon)
    ensure_history_schema(hcon)
    assert recent_runs(hcon) == []


def test_history_schema_is_not_named_after_the_database_file(hcon):
    """Regression test: the DuckDB file is guardian.duckdb, so a schema also
    called 'guardian' made every guardian.run_history reference ambiguous
    ('use a fully qualified path like guardian.guardian') and broke every
    run. The schema name must stay distinct from the catalog name."""
    from agent.history import HISTORY_SCHEMA
    assert HISTORY_SCHEMA != "guardian"


# --- persistence -----------------------------------------------------------

def test_record_run_persists_score_and_counts(hcon):
    record_run(hcon, "r1", health(88.5, warning=2), [a_finding(), a_finding()])
    runs = recent_runs(hcon)
    assert len(runs) == 1
    assert runs[0].health_score == 88.5
    assert runs[0].total_findings == 2


def test_record_run_persists_every_finding(hcon):
    record_run(hcon, "r1", health(90.0), [a_finding("not_null"), a_finding("uniqueness")])
    stored = hcon.execute(
        "select check_name from guardian_history.run_findings order by check_name"
    ).fetchall()
    assert [row[0] for row in stored] == ["not_null", "uniqueness"]


def test_recent_runs_are_returned_newest_first(hcon):
    now = datetime.now(timezone.utc)
    record_run(hcon, "old", health(95.0), [], now=now - timedelta(hours=2))
    record_run(hcon, "new", health(80.0), [], now=now)
    assert [r.run_id for r in recent_runs(hcon)] == ["new", "old"]


def test_row_counts_accumulate_across_runs(hcon):
    """This series is exactly what the z-score check consumes, so ordering
    and completeness matter."""
    now = datetime.now(timezone.utc)
    for i, count in enumerate([100, 110, 105]):
        record_run(hcon, f"r{i}", health(95.0), [], {"raw.crm_deals": count},
                   now=now + timedelta(minutes=i))
    assert historical_row_counts(hcon, "raw.crm_deals") == [100, 110, 105]


def test_historical_row_counts_can_exclude_the_current_run(hcon):
    """A run must not compare itself against itself -- that would drag the
    mean toward the very value being tested."""
    record_run(hcon, "r1", health(95.0), [], {"raw.crm_deals": 100})
    record_run(hcon, "r2", health(95.0), [], {"raw.crm_deals": 900})
    assert historical_row_counts(hcon, "raw.crm_deals", exclude_run_id="r2") == [100]


def test_run_ids_are_unique_and_sortable(hcon):
    first, second = new_run_id(), new_run_id()
    assert first != second and second > first


# --- degradation -----------------------------------------------------------

def test_no_degradation_finding_without_a_previous_run():
    """The first run ever has nothing to compare to and must not invent a
    trend from a single point."""
    assert check_health_degradation(50.0, None) is None


def test_small_drop_is_not_flagged():
    previous = RunRecord("r1", datetime.now(timezone.utc), 95.0, "HEALTHY", 1, 0)
    assert check_health_degradation(90.0, previous, threshold=15.0) is None


def test_large_drop_is_flagged_as_warning():
    previous = RunRecord("r1", datetime.now(timezone.utc), 95.0, "HEALTHY", 1, 0)
    result = check_health_degradation(75.0, previous, threshold=15.0)
    assert result is not None
    assert result.severity is Severity.WARNING


def test_very_large_drop_escalates_to_critical():
    """A 20-point slide is a warning; a collapse is a different event and
    should page differently."""
    previous = RunRecord("r1", datetime.now(timezone.utc), 98.0, "HEALTHY", 0, 0)
    result = check_health_degradation(30.0, previous, threshold=15.0)
    assert result.severity is Severity.CRITICAL
    assert result.details["drop"] == pytest.approx(68.0)


def test_improvement_is_never_flagged():
    previous = RunRecord("r1", datetime.now(timezone.utc), 40.0, "UNHEALTHY", 9, 5)
    assert check_health_degradation(95.0, previous) is None


# --- trend summary ---------------------------------------------------------

def test_trend_summary_is_empty_for_no_runs():
    assert summarize_trend([])["runs"] == 0

def test_trend_summary_compares_current_against_prior_mean():
    now = datetime.now(timezone.utc)
    runs = [
        RunRecord("r3", now, 70.0, "DEGRADED", 3, 1),
        RunRecord("r2", now, 90.0, "HEALTHY", 1, 0),
        RunRecord("r1", now, 100.0, "HEALTHY", 0, 0),
    ]
    summary = summarize_trend(runs)
    assert summary["mean_prior_score"] == pytest.approx(95.0)
    assert summary["delta_vs_mean"] == pytest.approx(-25.0)
    assert summary["best_score"] == 100.0
    assert summary["worst_score"] == 70.0

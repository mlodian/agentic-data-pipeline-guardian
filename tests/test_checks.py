from agent.checks import (
    check_row_count_zscore,
    check_freshness,
    check_not_null,
    check_referential_integrity,
    check_row_count_anomaly,
    check_unique,
    check_value_range,
    detect_pii_columns,
    diff_schema,
    snapshot_schema,
)
from agent.models import Severity


def test_check_not_null_passes_when_clean(con):
    con.execute("CREATE TABLE raw.t AS SELECT * FROM (VALUES (1), (2), (3)) AS t(id)")
    assert check_not_null(con, "raw.t", "id") is None


def test_check_not_null_flags_nulls(con):
    con.execute("CREATE TABLE raw.t AS SELECT * FROM (VALUES (1), (NULL), (3)) AS t(id)")
    finding = check_not_null(con, "raw.t", "id")
    assert finding is not None
    assert finding.check_name == "not_null"
    assert finding.details["nulls"] == 1


def test_check_not_null_severity_scales_with_rate(con):
    # 1 null out of 3 rows = 33% -> should be CRITICAL (well above the 1% cutoff)
    con.execute("CREATE TABLE raw.t AS SELECT * FROM (VALUES (1), (NULL), (3)) AS t(id)")
    finding = check_not_null(con, "raw.t", "id")
    assert finding.severity == Severity.CRITICAL


def test_check_unique_passes_when_no_duplicates(con):
    con.execute("CREATE TABLE raw.t AS SELECT * FROM (VALUES (1), (2), (3)) AS t(id)")
    assert check_unique(con, "raw.t", "id") is None


def test_check_unique_flags_duplicates(con):
    con.execute("CREATE TABLE raw.t AS SELECT * FROM (VALUES (1), (1), (2)) AS t(id)")
    finding = check_unique(con, "raw.t", "id")
    assert finding is not None
    assert finding.severity == Severity.CRITICAL
    assert finding.details["duplicates"] == 1


def test_referential_integrity_passes_when_all_match(con):
    con.execute("CREATE TABLE raw.parent AS SELECT * FROM (VALUES (1), (2)) AS t(id)")
    con.execute("CREATE TABLE raw.child AS SELECT * FROM (VALUES (1), (2)) AS t(parent_id)")
    assert check_referential_integrity(con, "raw.child", "parent_id", "raw.parent", "id") is None


def test_referential_integrity_flags_orphans(con):
    con.execute("CREATE TABLE raw.parent AS SELECT * FROM (VALUES (1)) AS t(id)")
    con.execute("CREATE TABLE raw.child AS SELECT * FROM (VALUES (1), (999)) AS t(parent_id)")
    finding = check_referential_integrity(con, "raw.child", "parent_id", "raw.parent", "id")
    assert finding is not None
    assert finding.details["orphans"] == 1


def test_check_value_range_flags_negative(con):
    con.execute("CREATE TABLE raw.t AS SELECT * FROM (VALUES (10.0), (-5.0)) AS t(amount)")
    finding = check_value_range(con, "raw.t", "amount", min_value=0)
    assert finding is not None
    assert finding.details["violations"] == 1


def test_check_value_range_passes_when_in_bounds(con):
    con.execute("CREATE TABLE raw.t AS SELECT * FROM (VALUES (10.0), (5.0)) AS t(amount)")
    assert check_value_range(con, "raw.t", "amount", min_value=0) is None


def test_check_freshness_flags_stale_data(con):
    con.execute(
        "CREATE TABLE raw.t AS SELECT current_timestamp - INTERVAL '10 days' AS ingested_at"
    )
    finding = check_freshness(con, "raw.t", "ingested_at", max_age_hours=48)
    assert finding is not None
    assert finding.check_name == "freshness"


def test_check_freshness_passes_when_recent(con):
    con.execute("CREATE TABLE raw.t AS SELECT current_timestamp AS ingested_at")
    assert check_freshness(con, "raw.t", "ingested_at", max_age_hours=48) is None


def test_row_count_anomaly_flags_large_deviation(con):
    con.execute("CREATE TABLE raw.t AS SELECT * FROM range(10) AS t(id)")
    finding = check_row_count_anomaly(con, "raw.t", baseline_count=100, tolerance_pct=0.5)
    assert finding is not None


def test_row_count_anomaly_passes_within_tolerance(con):
    con.execute("CREATE TABLE raw.t AS SELECT * FROM range(95) AS t(id)")
    assert check_row_count_anomaly(con, "raw.t", baseline_count=100, tolerance_pct=0.5) is None


def test_diff_schema_detects_dropped_column():
    baseline = {"customers": {"id": "BIGINT", "industry": "VARCHAR"}}
    current = {"customers": {"id": "BIGINT"}}
    findings = diff_schema(baseline, current)
    assert any(f.check_name == "schema_dropped_column" for f in findings)
    dropped = [f for f in findings if f.check_name == "schema_dropped_column"][0]
    assert dropped.severity == Severity.CRITICAL


def test_diff_schema_detects_new_column_as_info():
    baseline = {"customers": {"id": "BIGINT"}}
    current = {"customers": {"id": "BIGINT", "region": "VARCHAR"}}
    findings = diff_schema(baseline, current)
    assert findings[0].check_name == "schema_new_column"
    assert findings[0].severity == Severity.INFO


def test_diff_schema_detects_type_change():
    baseline = {"customers": {"id": "BIGINT"}}
    current = {"customers": {"id": "VARCHAR"}}
    findings = diff_schema(baseline, current)
    assert findings[0].check_name == "schema_type_changed"
    assert findings[0].severity == Severity.CRITICAL


def test_diff_schema_no_changes_no_findings():
    schema = {"customers": {"id": "BIGINT"}}
    assert diff_schema(schema, schema) == []


def test_detect_pii_columns_flags_email_and_name():
    schema = {"employees": {"employee_id": "BIGINT", "full_name": "VARCHAR", "email": "VARCHAR"}}
    findings = detect_pii_columns(schema)
    flagged_columns = {f.column for f in findings}
    assert flagged_columns == {"full_name", "email"}


def test_detect_pii_columns_ignores_non_pii():
    schema = {"deals": {"deal_id": "BIGINT", "amount": "DOUBLE"}}
    assert detect_pii_columns(schema) == []


def test_snapshot_schema_reflects_live_table(con):
    con.execute("CREATE TABLE raw.widgets (id BIGINT, name VARCHAR)")
    snapshot = snapshot_schema(con, schema="raw")
    assert snapshot["widgets"]["id"] == "BIGINT"
    assert snapshot["widgets"]["name"] == "VARCHAR"


# ---------------------------------------------------------------------------
# Statistical volume anomaly detection
#
# check_row_count_anomaly asks "did this move more than 50% from one
# baseline". check_row_count_zscore asks the better question -- "is this
# unusual for *this* table" -- which is what stops a naturally volatile feed
# alerting every run while a rock-steady one drifts unnoticed.
# ---------------------------------------------------------------------------

def _table_of_size(con, n: int):
    con.execute(f"create or replace table raw.vol as select * from range({n}) t(i)")


def test_zscore_returns_nothing_without_enough_history(con):
    """Two data points cannot produce a meaningful standard deviation, so the
    check stays silent rather than inventing confidence it doesn't have."""
    _table_of_size(con, 1000)
    assert check_row_count_zscore(con, "raw.vol", [990, 1010]) is None


def test_zscore_ignores_normal_variation(con):
    """A count sitting inside its own historical spread is not news."""
    _table_of_size(con, 1000)
    assert check_row_count_zscore(con, "raw.vol", [990, 1010, 1005, 995]) is None


def test_zscore_flags_a_count_far_outside_its_history(con):
    _table_of_size(con, 1000)
    finding = check_row_count_zscore(con, "raw.vol", [500, 510, 505, 495])
    assert finding is not None
    assert finding.check_name == "row_count_zscore"
    assert finding.details["z_score"] > 3


def test_zscore_flags_collapses_as_well_as_spikes(con):
    """A silent ingestion failure shows up as a count far *below* the mean;
    an absolute-value threshold is what catches both directions."""
    _table_of_size(con, 10)
    finding = check_row_count_zscore(con, "raw.vol", [1000, 1010, 990, 1005])
    assert finding is not None
    assert finding.details["z_score"] < 0
    assert "below" in finding.message


def test_zscore_severity_scales_with_how_extreme_the_deviation_is(con):
    """Three sigma is worth a look; past six is worth waking someone.
    History [900, 940, 920, 960] has mean 930 and sigma ~22, so 1019 rows is
    ~4 sigma (warning) and 1200 rows is ~12 sigma (critical)."""
    history = [900, 940, 920, 960]

    _table_of_size(con, 1019)
    mild = check_row_count_zscore(con, "raw.vol", history)
    assert mild.severity is Severity.WARNING

    _table_of_size(con, 1200)
    extreme = check_row_count_zscore(con, "raw.vol", history)
    assert extreme.severity is Severity.CRITICAL


def test_zscore_handles_a_perfectly_stable_history(con):
    """Zero variance would divide by zero. A table that has never moved is
    effectively a contract, so any movement warns rather than crashing."""
    _table_of_size(con, 1200)
    finding = check_row_count_zscore(con, "raw.vol", [900, 900, 900])
    assert finding is not None
    assert finding.severity is Severity.WARNING
    assert finding.details["z_score"] is None


def test_zscore_stays_silent_when_a_stable_table_does_not_move(con):
    _table_of_size(con, 900)
    assert check_row_count_zscore(con, "raw.vol", [900, 900, 900]) is None


# ---------------------------------------------------------------------------
# Resilience when the thing being checked has itself disappeared
#
# Regression tests for the failure that mattered most: an upstream table drop
# used to raise a CatalogException out of the foreign-key check and take down
# the entire run -- at exactly the moment the run was most needed.
# ---------------------------------------------------------------------------

def test_run_all_checks_survives_a_dropped_reference_table(con, tmp_path, monkeypatch):
    """The monitor must not die when the worst thing it monitors for happens."""
    from agent import run_checks as rc

    con.execute("create table raw.crm_deals as select * from (values (1, 10, 5.0)) t(deal_id, customer_id, amount)")
    monkeypatch.setattr(rc, "TABLE_CHECKS", {
        "raw.crm_deals": {
            "primary_key": "deal_id",
            "foreign_keys": [
                {"column": "customer_id", "ref_table": "raw.crm_customers", "ref_column": "customer_id"}
            ],
        }
    })
    monkeypatch.setattr(rc, "RECONCILIATION_RULES", [])
    monkeypatch.setattr(rc, "SCHEMA_BASELINE_PATH", str(tmp_path / "schema.json"))
    monkeypatch.setattr(rc, "ROW_COUNT_BASELINE_PATH", str(tmp_path / "counts.json"))

    findings = rc.run_all_checks(con)  # must not raise

    unverifiable = [f for f in findings if f.check_name == "foreign_key_unverifiable"]
    assert unverifiable, "a missing FK target should be reported, not silently skipped"
    assert unverifiable[0].severity is Severity.CRITICAL


def test_missing_reference_table_is_reported_rather_than_skipped(con, tmp_path, monkeypatch):
    """Losing the ability to verify a key is itself a finding: every row's key
    is now unvalidated, and a silent skip would look identical to a clean run."""
    from agent import run_checks as rc

    con.execute("create table raw.crm_deals as select * from (values (1, 10, 5.0)) t(deal_id, customer_id, amount)")
    monkeypatch.setattr(rc, "TABLE_CHECKS", {
        "raw.crm_deals": {
            "foreign_keys": [
                {"column": "customer_id", "ref_table": "raw.crm_customers", "ref_column": "customer_id"}
            ],
        }
    })
    monkeypatch.setattr(rc, "RECONCILIATION_RULES", [])
    monkeypatch.setattr(rc, "SCHEMA_BASELINE_PATH", str(tmp_path / "schema.json"))
    monkeypatch.setattr(rc, "ROW_COUNT_BASELINE_PATH", str(tmp_path / "counts.json"))

    finding = [f for f in rc.run_all_checks(con) if f.check_name == "foreign_key_unverifiable"][0]
    assert finding.details["ref_table"] == "raw.crm_customers"
    assert "no longer exists" in finding.message

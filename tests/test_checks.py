from agent.checks import (
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

"""Tests for cross-system reconciliation.

The behaviour worth protecting: clean data must reconcile silently (a check
that cries wolf gets switched off), and each distinct way two systems can
disagree must surface as its own finding with its own severity, because
"the feed never arrived" and "the amounts differ" have different fixes.
"""
import pytest

from agent.config import RECONCILIATION_RULES
from agent.models import Severity
from agent.reconciliation import reconcile, run_reconciliations

RULE = RECONCILIATION_RULES[0]


@pytest.fixture
def books(con):
    """A tiny two-system ledger: three closed-won deals, invoiced exactly."""
    con.execute("""
        create table raw.crm_deals as select * from (values
            (1, 'closed_won', 100.0),
            (2, 'closed_won', 200.0),
            (3, 'closed_won', 300.0),
            (4, 'prospecting', 999.0)
        ) t(customer_id, deal_stage, amount)
    """)
    con.execute("""
        create table raw.erp_invoices as select * from (values
            (1, 'paid', 100.0),
            (2, 'paid', 200.0),
            (3, 'unpaid', 300.0)
        ) t(customer_id, status, amount)
    """)
    return con


def names(findings):
    return {f.check_name for f in findings}


# --- the quiet case --------------------------------------------------------

def test_matching_books_produce_no_findings(books):
    """If this ever fails, every run cries wolf and the check becomes noise."""
    assert reconcile(books, RULE) == []


def test_open_pipeline_is_not_counted_as_revenue(books):
    """The prospecting deal has no invoice and must not be flagged -- only
    closed-won revenue is expected to appear in the ERP."""
    assert reconcile(books, RULE) == []


def test_split_invoices_still_reconcile(books):
    """Real billing splits one deal across several invoices, so the check has
    to compare summed totals per customer, not row against row."""
    books.execute("delete from raw.erp_invoices where customer_id = 3")
    books.execute("insert into raw.erp_invoices values (3, 'paid', 150.0), (3, 'paid', 150.0)")
    assert reconcile(books, RULE) == []


# --- coverage gaps ---------------------------------------------------------

def test_missing_invoices_are_a_coverage_finding(books):
    """CRM booked it, ERP never heard of it: a dropped feed, not a maths
    problem."""
    books.execute("delete from raw.erp_invoices where customer_id = 2")
    findings = reconcile(books, RULE)
    coverage = [f for f in findings if f.check_name == "reconciliation_coverage"]
    assert coverage
    assert coverage[0].severity is Severity.CRITICAL
    assert coverage[0].details["missing_in_right_count"] == 1


def test_invoice_without_a_matching_deal_is_lower_severity(books):
    """Billing something with no closed deal behind it is wrong, but it is a
    reporting problem rather than lost revenue -- so it warns, not pages."""
    books.execute("insert into raw.erp_invoices values (99, 'paid', 50.0)")
    findings = reconcile(books, RULE)
    coverage = [f for f in findings if f.check_name == "reconciliation_coverage"]
    assert coverage[0].severity is Severity.WARNING
    assert coverage[0].details["missing_in_left_count"] == 1


# --- amount mismatches -----------------------------------------------------

def test_amount_mismatch_beyond_tolerance_is_flagged(books):
    books.execute("update raw.erp_invoices set amount = 120.0 where customer_id = 1")
    totals = [f for f in reconcile(books, RULE) if f.check_name == "reconciliation_totals"]
    assert totals
    assert totals[0].details["mismatched_count"] == 1


def test_rounding_within_tolerance_is_ignored(books):
    """A cent of rounding on a split invoice is not an incident."""
    books.execute("update raw.erp_invoices set amount = 100.01 where customer_id = 1")
    assert [f for f in reconcile(books, RULE) if f.check_name == "reconciliation_totals"] == []


def test_voided_invoices_count_as_missing_revenue(books):
    """A void in the ERP against a still-closed-won deal in the CRM is the
    exact scenario the rule's status filter exists for."""
    books.execute("update raw.erp_invoices set status = 'void' where customer_id = 1")
    assert reconcile(books, RULE) != []


def test_worst_offenders_are_reported_largest_variance_first(books):
    """The report shows only the top few, so ordering decides what a human
    actually sees."""
    books.execute("update raw.erp_invoices set amount = 99.0 where customer_id = 1")
    books.execute("update raw.erp_invoices set amount = 100.0 where customer_id = 3")
    totals = [f for f in reconcile(books, RULE) if f.check_name == "reconciliation_totals"][0]
    variances = [o["variance"] for o in totals.details["worst_offenders"]]
    assert variances == sorted(variances, reverse=True)


def test_aggregate_variance_drives_severity(books):
    """One customer off by a rounding error is a warning; the books being
    materially wrong overall is critical."""
    books.execute("update raw.erp_invoices set amount = 10.0 where customer_id = 3")
    totals = [f for f in reconcile(books, RULE) if f.check_name == "reconciliation_totals"][0]
    assert totals.severity is Severity.CRITICAL


def test_both_failure_modes_can_fire_in_the_same_run(books):
    """A partial load usually produces both: some rows missing, others wrong.
    They must not mask each other."""
    books.execute("delete from raw.erp_invoices where customer_id = 1")
    books.execute("update raw.erp_invoices set amount = 500.0 where customer_id = 2")
    assert names(reconcile(books, RULE)) == {
        "reconciliation_coverage", "reconciliation_totals",
    }


def test_run_reconciliations_executes_every_configured_rule(books):
    assert run_reconciliations(books, []) == []
    books.execute("delete from raw.erp_invoices where customer_id = 1")
    assert run_reconciliations(books, RECONCILIATION_RULES)

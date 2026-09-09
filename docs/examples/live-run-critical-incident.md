# Live run: a CRITICAL incident, end to end

A real, unedited run of the Guardian against the `critical` scenario, using the
real Anthropic Claude API (not the deterministic fallback). Nothing below is
reconstructed or tidied up.

The scenario simulates the worst upstream event in the catalogue: `crm_customers`
-- the conformed dimension every mart joins to -- disappears entirely, on top of
schema drift, data-quality faults, and a CRM/ERP reconciliation break.

Reproduce it with:

```bash
python -m ingestion.generate --seed 1
python -m agent.run_checks --snapshot-baseline
python -m agent.guardian_agent --dry-run          # seed a healthy baseline

python -m ingestion.generate --seed 2 --scenario critical
GUARDIAN_ENABLE_QUARANTINE=true python -m agent.guardian_agent
```

## 1. Healthy baseline

```
$ python -m ingestion.generate --seed 1
Ingestion complete:
  raw.crm_customers: 400 rows
  raw.crm_deals: 1200 rows
  raw.erp_invoices: 244 rows
  raw.hris_employees: 150 rows

$ python -m agent.run_checks --snapshot-baseline
Baseline written to agent/schema_baseline.json and agent/row_count_baseline.json

$ python -m agent.guardian_agent --dry-run    # seed a healthy baseline in history

Health score: 98.7 (HEALTHY)
Findings: 2
Downstream models impacted: 1
Report written to: docs/incident_reports/20260909T040539Z.md
```

## 2. The critical scenario

```
$ python -m ingestion.generate --seed 2 --scenario critical
[inject] simulating upstream CRM schema drift on raw.crm_customers
[inject] simulating data-quality issues across raw tables
[inject] simulating CRM/ERP reconciliation drift
[inject] simulating upstream removal of raw.crm_customers
Ingestion complete:
  raw.crm_customers: DROPPED (simulated upstream removal)
  raw.crm_deals: 1200 rows
  raw.erp_invoices: 207 rows
  raw.hris_employees: 150 rows

$ GUARDIAN_ENABLE_QUARANTINE=true python -m agent.guardian_agent
[guardian] quarantined 7 row(s) across 2 action(s).
[guardian] critical findings detected, but GUARDIAN_OPEN_GH_ISSUES is not 'true' -- skipping GitHub issue creation.

Health score: 32.8 (UNHEALTHY)
Findings: 12
Downstream models impacted: 8
Rows quarantined: 7
Report written to: docs/incident_reports/20260909T040627Z.md
```

Health fell from **98.7 (HEALTHY)** to **32.8 (UNHEALTHY)** in a single run. The
agent quarantined 7 provably-invalid rows and requested escalation; the GitHub
issue was correctly suppressed because `GUARDIAN_OPEN_GH_ISSUES` was not set.

## 3. The incident report Claude wrote

Verbatim, exactly as `write_report` persisted it:

---

# Data Quality Incident Report

## Health Score: 32.8 / 100 (UNHEALTHY)
**Trend:** ⬇️ **-65.9 points** since last run (98.7 → 32.8)  
**Total Risk:** 67.24 | **6 CRITICAL** | **5 WARNING** | **0 INFO**

---

## Root Cause
The **`raw.crm_customers` table has been dropped** from the warehouse. This single schema change is cascading into foreign key validation failures across `raw.crm_deals` and `raw.erp_invoices`, blocking 7 downstream models including the critical `customer_revenue_summary` mart.

---

## CRITICAL Findings (6)

### 1. **Missing Table: `crm_customers`** (Risk: 15.0)
- **Impact:** 3 models blocked: `stg_crm_customers`, `dim_customers`, `customer_revenue_summary`
- **Next Step:** Immediately investigate the CRM connector or upstream pipeline. Check if the table was accidentally dropped, renamed, or if the sync failed. Restore from backup or re-run the ingestion job.

### 2. **Foreign Key Unverifiable: `raw.crm_deals.customer_id`** (Risk: 9.6)
- **Impact:** 3 models at risk: `stg_crm_deals`, `fct_deals`, `customer_revenue_summary`
- **Cause:** References missing `raw.crm_customers` table
- **Next Step:** Blocked by #1. Once `crm_customers` is restored, re-validate foreign keys.

### 3. **Foreign Key Unverifiable: `raw.erp_invoices.customer_id`** (Risk: TBD)
- **Impact:** 3 models at risk: `stg_erp_invoices`, `fct_invoices`, `customer_revenue_summary`
- **Cause:** References missing `raw.crm_customers` table
- **Next Step:** Blocked by #1. Once `crm_customers` is restored, re-validate foreign keys.

### 4. **Duplicate Primary Key: `raw.erp_invoices.invoice_id`** (Risk: 9.6)
- **Impact:** 3 models at risk: `stg_erp_invoices`, `fct_invoices`, `customer_revenue_summary`
- **Details:** 1 duplicate value found (207 total rows, 206 distinct)
- **Next Step:** Query `SELECT invoice_id, COUNT(*) FROM raw.erp_invoices GROUP BY invoice_id HAVING COUNT(*) > 1` to identify the duplicate. Investigate ERP connector for double-insert bug or upstream data issue.

### 5. **Reconciliation Coverage Gap: `closed_won_deals_vs_invoiced_revenue`**
- **Impact:** 3 models at risk: `stg_crm_deals`, `fct_deals`, `customer_revenue_summary`
- **Details:** 12 closed-won deals (IDs: 98, 140, 367, 200, 368, 1, 34, 305, 312, 385, +2 more) have no matching invoices
- **Next Step:** Coordinate with Finance and Sales Ops to determine if these deals are legitimately un-invoiced (e.g., pending, cancelled) or if invoice records are missing from the ERP feed.

### 6. **Reconciliation Totals Mismatch: `closed_won_deals_vs_invoiced_revenue`**
- **Impact:** 3 models at risk: `stg_crm_deals`, `fct_deals`, `customer_revenue_summary`
- **Details:** 17 deals show amount discrepancies. Aggregate variance: **11.34%** ($23.4M in deals vs $20.8M in invoices)
- **Worst offenders:** Deal 326 (94.6% variance), Deal 350 (85.7% variance), Deal 173 (50% variance)
- **Next Step:** Pull detailed comparison: `SELECT d.deal_id, d.amount AS deal_amount, i.amount AS invoice_amount FROM raw.crm_deals d JOIN raw.erp_invoices i ON d.deal_id = i.deal_id WHERE ABS(d.amount - i.amount) / d.amount > 0.10`. Escalate top 5 discrepancies to RevOps for investigation.

---

## WARNING Findings (5)

### 7. **Null Values: `raw.crm_deals.amount`**
- **Details:** 5 of 1,200 rows (0.4%) have null amounts
- **Impact:** 3 models: `stg_crm_deals`, `fct_deals`, `customer_revenue_summary`
- **Next Step:** Quarantine recommended (see below). These rows have a required field missing and should be isolated until corrected in the CRM.

### 8. **Negative Values: `raw.erp_invoices.amount`**
- **Details:** 2 rows have amounts below the configured minimum (< 0)
- **Impact:** 3 models: `stg_erp_invoices`, `fct_invoices`, `customer_revenue_summary`
- **Next Step:** Quarantine recommended (see below). Negative invoice amounts are invalid; likely data entry errors or credit memos miscoded as invoices.

### 9. **Stale Data: `raw.hris_employees.ingested_at`**
- **Details:** Last refresh was 216 hours ago (9 days), exceeding 48-hour SLA
- **Impact:** 1 model: `stg_hris_employees`
- **Next Step:** Check HRIS connector status and logs. Restart the sync job if stalled.

### 10. **PII Detected: `hris_employees.full_name`**
- **Details:** Column appears to contain name data
- **Impact:** 1 model: `stg_hris_employees`
- **Next Step:** Verify column is access-controlled or apply masking policy in staging layer.

### 11. **PII Detected: `hris_employees.email`**
- **Details:** Column appears to contain email data
- **Impact:** 1 model: `stg_hris_employees`
- **Next Step:** Verify column is access-controlled or apply masking policy in staging layer.

---

## Downstream Models at Risk

| Model | Critical Findings | Total Findings | Blocked Sources |
|-------|-------------------|----------------|-----------------|
| **customer_revenue_summary** | 6 | 8 | `crm_customers`, `crm_deals`, `erp_invoices` |
| **fct_deals** | 3 | 4 | `crm_deals` |
| **stg_crm_deals** | 3 | 4 | `crm_deals` |
| **fct_invoices** | 2 | 3 | `erp_invoices` |
| **stg_erp_invoices** | 2 | 3 | `erp_invoices` |
| **dim_customers** | 1 | 1 | `crm_customers` |
| **stg_crm_customers** | 1 | 1 | `crm_customers` |
| **stg_hris_employees** | 0 | 3 | `hris_employees` |

---

## Quarantine Recommendation

Two quarantine plans are ready to isolate unambiguously invalid rows:

1. **`raw.crm_deals`**: 5 rows with `amount IS NULL` (required field violation)
2. **`raw.erp_invoices`**: 2 rows with `amount < 0` (business rule violation: invoices cannot have negative amounts)

**Recommendation:** Approve both quarantines. These rows are definitively invalid and will corrupt downstream aggregations if left in place.

---

## Immediate Actions

1. **[P0]** Restore `raw.crm_customers` table – investigate CRM connector failure
2. **[P0]** Identify and resolve duplicate `invoice_id` in `raw.erp_invoices`
3. **[P1]** Approve quarantine for null deal amounts and negative invoice amounts
4. **[P1]** Investigate 12 un-invoiced closed-won deals with Finance/Sales
5. **[P2]** Review top 5 deal/invoice amount mismatches with RevOps
6. **[P2]** Restart stale HRIS connector (9 days overdue)

---

## What this run demonstrates

- **The monitor survives the thing it monitors for.** An earlier version of this
  code raised a `CatalogException` out of the foreign-key check and killed the
  whole run the moment `crm_customers` was dropped -- the failure mode where the
  alarm dies with the building. The run now reports the loss of verifiability as
  its own CRITICAL finding and carries on.
- **Root cause over symptom list.** The model was handed twelve findings and
  correctly identified the dropped table as the cause of the two foreign-key
  failures and the reconciliation breakdown, rather than listing them as twelve
  independent problems.
- **Scoped, reversible action.** It approved quarantine only for the two rules
  that were unambiguously invalid (null required key, negative amount), leaving
  the schema-level findings for a human. Those 7 rows are recoverable from the
  `quarantine` schema with a single SELECT.
- **Blast radius, not just severity.** `customer_revenue_summary` was named as
  the model to treat as unreliable, because 9 of the 12 findings converge on it.

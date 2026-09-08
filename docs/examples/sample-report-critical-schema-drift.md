# Guardian Report (deterministic mode)

- [INFO] crm_customers.industry_segment: New column 'industry_segment' (VARCHAR) appeared on 'crm_customers'.
- [INFO] crm_customers.lifecycle_stage: New column 'lifecycle_stage' (VARCHAR) appeared on 'crm_customers'.
- [CRITICAL] crm_customers.industry: Column 'industry' (VARCHAR) existed on 'crm_customers' in the baseline but is gone now -- likely an upstream schema change or rename.
- [CRITICAL] crm_customers.is_active: Column 'is_active' (BOOLEAN) existed on 'crm_customers' in the baseline but is gone now -- likely an upstream schema change or rename.
- [WARNING] hris_employees.full_name: Column 'full_name' on 'hris_employees' looks like it holds name data. Confirm it's masked or access-controlled.
- [WARNING] hris_employees.email: Column 'email' on 'hris_employees' looks like it holds email data. Confirm it's masked or access-controlled.
- [WARNING] raw.crm_deals.amount: 5/1200 rows (0.4%) have a null 'amount'.
- [CRITICAL] raw.crm_deals.customer_id: 3 row(s) in raw.crm_deals reference a customer_id that doesn't exist in raw.crm_customers.customer_id.
- [CRITICAL] raw.erp_invoices.invoice_id: 'invoice_id' on raw.erp_invoices is not unique: 2 duplicate value(s) found.
- [WARNING] raw.erp_invoices.amount: 2 row(s) in raw.erp_invoices.amount fall outside the expected range.
- [WARNING] raw.hris_employees.ingested_at: Most recent ingested_at in raw.hris_employees is 216h old, exceeding the 48h freshness SLA -- the connector may have stalled.

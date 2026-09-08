select
    i.invoice_id,
    i.deal_id,
    i.customer_id,
    i.invoice_date,
    i.due_date,
    i.amount,
    i.status,
    i.ingested_at
from {{ ref('stg_erp_invoices') }} as i

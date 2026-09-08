with source as (
    select * from {{ source('raw', 'erp_invoices') }}
)

select
    invoice_id,
    customer_id,
    invoice_date,
    due_date,
    amount,
    status,
    ingested_at
from source

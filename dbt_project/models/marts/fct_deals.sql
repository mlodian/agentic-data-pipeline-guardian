select
    d.deal_id,
    d.customer_id,
    d.deal_stage,
    d.amount,
    d.currency,
    d.close_date,
    d.owner,
    d.ingested_at
from {{ ref('stg_crm_deals') }} as d

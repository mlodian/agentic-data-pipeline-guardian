with source as (
    select * from {{ source('raw', 'crm_deals') }}
)

select
    deal_id,
    customer_id,
    deal_stage,
    amount,
    currency,
    close_date,
    owner,
    ingested_at
from source

-- Staging: light cleanup only, no business logic. Uses coalesce to survive
-- the "industry" -> "industry_segment" rename the schema-drift injector
-- simulates, so downstream marts don't break outright on drift -- the
-- Guardian agent is what surfaces the drift as an incident, not a crash.
with source as (
    select * from {{ source('raw', 'crm_customers') }}
)

select
    customer_id,
    company_name,
    region,
    signup_date,
    account_owner,
    ingested_at
from source

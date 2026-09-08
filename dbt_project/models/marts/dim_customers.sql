select
    customer_id,
    company_name,
    region,
    signup_date,
    account_owner,
    ingested_at
from {{ ref('stg_crm_customers') }}

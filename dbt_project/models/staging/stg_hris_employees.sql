with source as (
    select * from {{ source('raw', 'hris_employees') }}
)

select
    employee_id,
    full_name,
    email,
    department,
    hire_date,
    manager_id,
    is_active,
    ingested_at
from source
